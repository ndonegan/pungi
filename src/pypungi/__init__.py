#!/usr/bin/python -tt


# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.


import yum
import os
import re
import shutil
import sys
import gzip
import pypungi.util
import logging
import urlgrabber.progress
import subprocess
import createrepo
import ConfigParser
import pylorax
from fnmatch import fnmatch

import arch as arch_module
import multilib


def is_debug(po):
    if "debuginfo" in po.name:
        return True
    return False


def is_source(po):
    if po.arch in ("src", "nosrc"):
        return True
    return False


def is_noarch(po):
    if po.arch == "noarch":
        return True
    return False


def is_package(po):
    if is_debug(po):
        return False
    if is_source(po):
        return False
    return True


class MyConfigParser(ConfigParser.ConfigParser):
    """A subclass of ConfigParser which does not lowercase options"""

    def optionxform(self, optionstr):
        return optionstr


class PungiBase(object):
    """The base Pungi class.  Set up config items and logging here"""

    def __init__(self, config):
        self.config = config

        # ARCH setup
        self.tree_arch = self.config.get('pungi', 'arch')
        self.yum_arch = arch_module.tree_arch_to_yum_arch(arch)
        full_archlist = self.config.getboolean('pungi', 'full_archlist')
        self.valid_arches = arch_module.get_valid_arches(self.tree_arch, multilib=full_archlist)
        self.valid_arches.append("src") # throw source in there, filter it later
        self.valid_native_arches = arch_module.get_valid_arches(self.tree_arch, multilib=False)
        self.valid_multilib_arches = arch_module.get_valid_multilib_arches(self.tree_arch)

        # --nogreedy
        self.greedy = config.getboolean('pungi', 'alldeps')

        # arch: compatible arches
        self.compatible_arches = {}
        for i in self.valid_arches:
            self.compatible_arches[i] = arch_module.get_compatible_arches(i)

        self.doLoggerSetup()
        self.workdir = os.path.join(self.config.get('pungi', 'destdir'),
                                    'work',
                                    self.config.get('pungi', 'flavor'),
                                    self.tree_arch)



    def doLoggerSetup(self):
        """Setup our logger"""

        logdir = os.path.join(self.config.get('pungi', 'destdir'), 'logs')

        pypungi.util._ensuredir(logdir, None, force=True) # Always allow logs to be written out

        if self.config.get('pungi', 'flavor'):
            logfile = os.path.join(logdir, '%s.%s.log' % (self.config.get('pungi', 'flavor'),
                                                          self.tree_arch))
        else:
            logfile = os.path.join(logdir, '%s.log' % (self.tree_arch))

        # Create the root logger, that will log to our file
        logging.basicConfig(level=logging.DEBUG,
                            format='%(name)s.%(levelname)s: %(message)s',
                            filename=logfile)


class CallBack(urlgrabber.progress.TextMeter):
    """A call back function used with yum."""

    def progressbar(self, current, total, name=None):
        return


class PungiYum(yum.YumBase):
    """Subclass of Yum"""

    def __init__(self, config):
        self.pungiconfig = config
        yum.YumBase.__init__(self)

    def _checkInstall(self, txmbr):
        # overriding this method allows us to ignore installed packages
        # and always prefer native packages over those who pull less deps into a transaction
        return []

    def doLoggingSetup(self, debuglevel, errorlevel, syslog_ident=None, syslog_facility=None):
        """Setup the logging facility."""

        logdir = os.path.join(self.pungiconfig.get('pungi', 'destdir'), 'logs')
        if not os.path.exists(logdir):
            os.makedirs(logdir)
        if self.pungiconfig.get('pungi', 'flavor'):
            logfile = os.path.join(logdir, '%s.%s.log' % (self.pungiconfig.get('pungi', 'flavor'),
                                                          self.pungiconfig.get('pungi', 'arch')))
        else:
            logfile = os.path.join(logdir, '%s.log' % (self.pungiconfig.get('pungi', 'arch')))

        yum.logging.basicConfig(level=yum.logging.DEBUG, filename=logfile)

    def doFileLogSetup(self, uid, logfile):
        # This function overrides a yum function, allowing pungi to control
        # the logging.
        pass


class Pungi(pypungi.PungiBase):
    def __init__(self, config, ksparser):
        pypungi.PungiBase.__init__(self, config)
 
        # Set our own logging name space
        self.logger = logging.getLogger('Pungi')

        # Create the stdout/err streams and only send INFO+ stuff there
        formatter = logging.Formatter('%(name)s:%(levelname)s: %(message)s')
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(logging.INFO)
        self.logger.addHandler(console)

        self.destdir = self.config.get('pungi', 'destdir')
        self.archdir = os.path.join(self.destdir,
                                   self.config.get('pungi', 'version'),
                                   self.config.get('pungi', 'flavor'),
                                   self.tree_arch)

        self.topdir = os.path.join(self.archdir, 'os')
        self.isodir = os.path.join(self.archdir, self.config.get('pungi','isodir'))

        pypungi.util._ensuredir(self.workdir, self.logger, force=True)

        self.common_files = []
        self.infofile = os.path.join(self.config.get('pungi', 'destdir'),
                                    self.config.get('pungi', 'version'),
                                    '.composeinfo')


        self.ksparser = ksparser
        self.polist = []
        self.srpmpolist = []
        self.debuginfolist = []
        self.srpms_build = []
        self.srpms_fulltree = []
        self.last_po = 0
        self.resolved_deps = {} # list the deps we've already resolved, short circuit.
        self.excluded_pkgs = {} # list the packages we've already excluded.
        self.seen_pkgs = {}     # list the packages we've already seen so we can check all deps only once
        self.multilib_methods = self.config.get('pungi', 'multilib').split(" ")
        self.lookaside_repos = self.config.get('pungi', 'lookaside_repos').split(" ")
        self.sourcerpm_arch_map = {}    # {sourcerpm: set[arches]} - used for gathering debuginfo

    def _add_yum_repo(self, name, url, mirrorlist=False, groups=True,
                      cost=1000, includepkgs=[], excludepkgs=[],
                      proxy=None):
        """This function adds a repo to the yum object.

        name: Name of the repo

        url: Full url to the repo

        mirrorlist: Bool for whether or not url is a mirrorlist

        groups: Bool for whether or not to use groupdata from this repo

        cost: an optional int representing the cost of a repo

        includepkgs: An optional list of includes to use

        excludepkgs: An optional list of excludes to use

        proxy: An optional proxy to use

        """

        self.logger.info('Adding repo %s' % name)
        thisrepo = yum.yumRepo.YumRepository(name)
        thisrepo.name = name
        # add excludes and such here when pykickstart gets them
        if mirrorlist:
            thisrepo.mirrorlist = yum.parser.varReplace(url,
                                                        self.ayum.conf.yumvar)
            self.mirrorlists.append(thisrepo.mirrorlist)
            self.logger.info('Mirrorlist for repo %s is %s' %
                             (thisrepo.name, thisrepo.mirrorlist))
        else:
            thisrepo.baseurl = yum.parser.varReplace(url,
                                                     self.ayum.conf.yumvar)
            self.repos.extend(thisrepo.baseurl)
            self.logger.info('URL for repo %s is %s' %
                             (thisrepo.name, thisrepo.baseurl))
        thisrepo.basecachedir = self.ayum.conf.cachedir
        thisrepo.enablegroups = groups
        # This is until yum uses this failover by default
        thisrepo.failovermethod = 'priority'
        thisrepo.exclude = excludepkgs
        thisrepo.includepkgs = includepkgs
        thisrepo.cost = cost
        # Yum doesn't like proxy being None
        if proxy:
            thisrepo.proxy = proxy
        self.ayum.repos.add(thisrepo)
        self.ayum.repos.enableRepo(thisrepo.id)
        self.ayum._getRepos(thisrepo=thisrepo.id, doSetup=True)
        # Set the repo callback.
        self.ayum.repos.setProgressBar(CallBack())
        self.ayum.repos.callback = CallBack()
        thisrepo.metadata_expire = 0
        thisrepo.mirrorlist_expire = 0
        if os.path.exists(os.path.join(thisrepo.cachedir, 'repomd.xml')):
            os.remove(os.path.join(thisrepo.cachedir, 'repomd.xml'))

    def _inityum(self):
        """Initialize the yum object.  Only needed for certain actions."""

        # Create a yum object to use
        self.repos = []
        self.mirrorlists = []
        self.ayum = PungiYum(self.config)
        self.ayum.doLoggingSetup(6, 6)
        yumconf = yum.config.YumConf()
        yumconf.debuglevel = 6
        yumconf.errorlevel = 6
        yumconf.cachedir = self.config.get('pungi', 'cachedir')
        yumconf.persistdir = "/var/lib/yum" # keep at default, gets appended to installroot
        yumconf.installroot = os.path.join(self.workdir, 'yumroot')
        yumconf.uid = os.geteuid()
        yumconf.cache = 0
        yumconf.failovermethod = 'priority'
        yumconf.deltarpm = 0
        yumvars = yum.config._getEnvVar()
        yumvars['releasever'] = self.config.get('pungi', 'version')
        yumvars['basearch'] = yum.rpmUtils.arch.getBaseArch(myarch=self.tree_arch)
        yumconf.yumvar = yumvars
        self.ayum._conf = yumconf
        # I have no idea why this fixes a traceback, but James says it does.
        del self.ayum.prerepoconf
        self.ayum.repos.setCacheDir(self.ayum.conf.cachedir)

        # deal with our repos
        try:
            self.ksparser.handler.repo.methodToRepo()
        except:
            pass

        for repo in self.ksparser.handler.repo.repoList:
            if repo.mirrorlist:
                # The not bool() thing is because pykickstart is yes/no on
                # whether to ignore groups, but yum is a yes/no on whether to
                # include groups.  Awkward.
                self._add_yum_repo(repo.name, repo.mirrorlist,
                                   mirrorlist=True,
                                   groups=not bool(repo.ignoregroups),
                                   cost=repo.cost,
                                   includepkgs=repo.includepkgs,
                                   excludepkgs=repo.excludepkgs,
                                   proxy=repo.proxy)
            else:
                self._add_yum_repo(repo.name, repo.baseurl,
                                   mirrorlist=False,
                                   groups=not bool(repo.ignoregroups),
                                   cost=repo.cost,
                                   includepkgs=repo.includepkgs,
                                   excludepkgs=repo.excludepkgs,
                                   proxy=repo.proxy)

        self.logger.info('Getting sacks for arches %s' % self.valid_arches)
        self.ayum._getSacks(archlist=self.valid_arches)

    def _filtersrcdebug(self, po):
        """Filter out package objects that are of 'src' arch."""

        if po.arch == 'src' or 'debuginfo' in po.name:
            return False

        return True

    def add_package(self, po, msg=None):
        if not is_package(po):
            raise ValueError("Not a binary package: %s" % po)
        if msg:
            self.logger.info(msg)
        if po not in self.polist:
            self.polist.append(po)
        self.ayum.install(po)
        self.sourcerpm_arch_map.setdefault(po.sourcerpm, set()).add(po.arch)

    def add_debuginfo(self, po, msg=None):
        if not is_debug(po):
            raise ValueError("Not a debuginfog package: %s" % po)
        if msg:
            self.logger.info(msg)
        if po not in self.debuginfolist:
            self.debuginfolist.append(po)

    def add_source(self, po, msg=None):
        if not is_source(po):
            raise ValueError("Not a source package: %s" % po)
        if msg:
            self.logger.info(msg)
        if po not in self.srpmpolist:
            self.srpmpolist.append(po)

    def verifyCachePkg(self, po, path): # Stolen from yum
        """check the package checksum vs the cache
           return True if pkg is good, False if not"""

        (csum_type, csum) = po.returnIdSum()

        try:
            filesum = yum.misc.checksum(csum_type, path)
        except yum.Errors.MiscError:
            return False

        if filesum != csum:
            return False

        return True

    def excludePackages(self, pkg_sack):
        """exclude packages according to config file"""
        if not pkg_sack:
            return pkg_sack

        excludes = [] # list of (name, arch, pattern)
        for i in self.ksparser.handler.packages.excludedList:
            pattern = i
            multilib = False
            if i.endswith(".+"):
                multilib = True
                i = i[:-2]
            name, arch = arch_module.split_name_arch(i)
            excludes.append((name, arch, pattern, multilib))

        for pkg in pkg_sack[:]:
            for name, arch, exclude_pattern, multilib in excludes:
                if fnmatch(pkg.name, name):
                    if not arch or fnmatch(pkg.arch, arch):
                        if multilib and pkg.arch not in self.valid_multilib_arches:
                            continue
                        if pkg.nvra not in self.excluded_pkgs:
                            self.logger.info("Excluding %s.%s (pattern: %s)" % (pkg.name, pkg.arch, exclude_pattern))
                            self.excluded_pkgs[pkg.nvra] = pkg
                        pkg_sack.remove(pkg)
                        break

        return pkg_sack

    def getPackageDeps(self, po):
        """Add the dependencies for a given package to the
           transaction info"""
        if po in self.seen_pkgs:
            return
        self.seen_pkgs[po] = None

        self.logger.info('Checking deps of %s.%s' % (po.name, po.arch))

        reqs = po.requires
        provs = po.provides
        added = set()

        added.update(self.getLangpacks([po]))
        added.update(self.getMultilib([po]))

        for req in reqs:
            if req in self.resolved_deps:
                continue
            r, f, v = req
            if r.startswith('rpmlib(') or r.startswith('config('):
                continue
            if req in provs:
                continue

            try:
                deps = self.ayum.whatProvides(r, f, v).returnPackages()
                deps = self.excludePackages(deps)
                if not deps:
                    self.logger.warn("Unresolvable dependency %s in %s.%s" % (r, po.name, po.arch))
                    continue

                if self.greedy:
                    deps = yum.packageSack.ListPackageSack(deps).returnNewestByNameArch()
                else:
                    found = False
                    for dep in deps:
                        if dep in self.polist:
                            found = True
                            break
                    if found:
                        deps = []
                    else:
                        deps = [self.ayum._bestPackageFromList(deps)]

                for dep in deps:
                    if dep not in added:
                        msg = 'Added %s.%s for %s.%s' % (dep.name, dep.arch, po.name, po.arch)
                        self.add_package(dep, msg)
                        added.add(dep)

            except (yum.Errors.InstallError, yum.Errors.YumBaseError), ex:
                self.logger.warn("Unresolvable dependency %s in %s.%s" % (r, po.name, po.arch))
                continue
            self.resolved_deps[req] = None

        for add in added:
            self.getPackageDeps(add)

    def getLangpacks(self, po_list):
        added = []

        for po in po_list:
            # get all langpacks matching the package name
            langpacks = [ i for i in self.langpacks if i["name"] == po.name ]
            if not langpacks:
                continue

            for langpack in langpacks:
                pattern = langpack["install"] % "*" # replace '%s' with '*'
                exactmatched, matched, unmatched = yum.packages.parsePackages(self.pkgs, [pattern], casematch=1, pkgdict=self.pkg_refs.copy())
                matches = filter(self._filtersrcdebug, exactmatched + matched)
                matches = [ i for i in matches if not i.name.endswith("-devel") and not i.name.endswith("-static") and i.name != "man-pages-overrides" ]
                matches = [ i for i in matches if fnmatch(i.name, pattern) ]

                packages_by_name = {}
                for i in matches:
                    packages_by_name.setdefault(i.name, []).append(i)

                for i, pkg_sack in packages_by_name.iteritems():
                    pkg_sack = self.excludePackages(pkg_sack)
                    match = self.ayum._bestPackageFromList(pkg_sack)
                    msg = 'Added langpack %s.%s for package %s (pattern: %s)' % (match.name, match.arch, po.name, pattern)
                    self.add_package(match, msg)
                    added.append(match)

        return added

    def getMultilib(self, po_list):
        added = []

        if not self.multilib_methods:
            return added

        for po in po_list:
            if po.arch in ("noarch", "src", "nosrc"):
                continue

            if po.arch in self.valid_multilib_arches:
                continue

            matches = self.ayum.pkgSack.searchNevra(name=po.name, ver=po.version, rel=po.release)
            matches = [i for i in matches if i.arch in self.valid_multilib_arches]
            if not matches:
                continue
            matches = self.excludePackages(matches)
            match = self.ayum._bestPackageFromList(matches)
            if not match:
                continue
            method = multilib.po_is_multilib(po, self.multilib_methods)
            if not method:
                continue
            msg = "Added multilib package %s.%s for package %s.%s (method: %s)" % (match.name, match.arch, po.name, po.arch, method)
            self.add_package(match, msg)
            added.append(match)
        return added

    def getPackagesFromGroup(self, group):
        """Get a list of package names from a ksparser group object

            Returns a list of package names"""

        packages = []

        # Check if we have the group
        if not self.ayum.comps.has_group(group.name):
            self.logger.error("Group %s not found in comps!" % group)
            return packages

        # Get the group object to work with
        groupobj = self.ayum.comps.return_group(group.name)

        # Add the mandatory packages
        packages.extend(groupobj.mandatory_packages.keys())

        # Add the default packages unless we don't want them
        if group.include == 1:
            packages.extend(groupobj.default_packages.keys())

        # Add the optional packages if we want them
        if group.include == 2:
            packages.extend(groupobj.default_packages.keys())
            packages.extend(groupobj.optional_packages.keys())

        # Deal with conditional packages
        # Populate a dict with the name of the required package and value
        # of the package objects it would bring in.  To be used later if
        # we match the conditional.
        for condreq, cond in groupobj.conditional_packages.iteritems():
            pkgs = self.ayum.pkgSack.searchNevra(name=condreq)
            if pkgs:
                pkgs = self.ayum.bestPackagesFromList(pkgs, arch=self.ayum.compatarch)
            if self.ayum.tsInfo.conditionals.has_key(cond):
                self.ayum.tsInfo.conditionals[cond].extend(pkgs)
            else:
                self.ayum.tsInfo.conditionals[cond] = pkgs

        return packages

    def _addDefaultGroups(self, excludeGroups=[]):
        """Cycle through the groups and return at list of the ones that ara
           default."""

        # This is mostly stolen from anaconda.
        groups = map(lambda x: x.groupid,
            filter(lambda x: x.default, self.ayum.comps.groups))

        groups = [x for x in groups if x not in excludeGroups]

        self.logger.debug('Add default groups %s' % groups)
        return groups

    def getPackageObjects(self):
        """Cycle through the list of packages, get package object
           matches, and resolve deps.

           Returns a list of package objects"""

        final_pkgobjs = {} # The final list of package objects
        searchlist = [] # The list of package names/globs to search for
        matchdict = {} # A dict of objects to names
        excludeGroups = [] #A list of groups for removal defined in the ks file

        # precompute pkgs and pkg_refs to speed things up
        self.pkgs = self.ayum.pkgSack.returnPackages()
        self.pkg_refs = yum.packages.buildPkgRefDict(self.pkgs, casematch=True)

        try:
            self.langpacks = list(self.ayum.comps.langpacks)
        except AttributeError:
            # old yum
            self.logger.warning("Could not get langpacks via yum.comps. You may need to update yum.")
            self.langpacks = []
        except yum.Errors.GroupsError:
            # no groups or no comps at all
            self.logger.warning("Could not get langpacks due to missing comps in repodata or --ignoregroups=true option.")
            self.langpacks = []

        # First remove the excludes
        self.ayum.excludePackages()
        
        # Get the groups set for removal
        for group in self.ksparser.handler.packages.excludedGroupList:
            excludeGroups.append(str(group)[1:])

        # Always add the core group
        self.ksparser.handler.packages.add(['@core'])

        # Check to see if we want all the defaults
        if self.ksparser.handler.packages.default:
            for group in self._addDefaultGroups(excludeGroups):
                self.ksparser.handler.packages.add(['@%s' % group])

        # Check to see if we need the base group
        if self.ksparser.handler.packages.addBase:
            self.ksparser.handler.packages.add(['@base'])

        # Get a list of packages from groups
        for group in self.ksparser.handler.packages.groupList:
            searchlist.extend(self.getPackagesFromGroup(group))

        # Add the adds
        searchlist.extend(self.ksparser.handler.packages.packageList)

        # Make the search list unique
        searchlist = yum.misc.unique(searchlist)

        for name in searchlist:
            pattern = name
            multilib = False
            if name.endswith(".+"):
                name = name[:-2]
                multilib = True

            if self.greedy and name == "system-release":
                # HACK: handles a special case, when system-release virtual provide is specified in the greedy mode
                matches = self.ayum.whatProvides(name, None, None).returnPackages()
            else:
                exactmatched, matched, unmatched = yum.packages.parsePackages(self.pkgs, [name], casematch=1, pkgdict=self.pkg_refs.copy())
                matches = exactmatched + matched

            matches = filter(self._filtersrcdebug, matches)

            if multilib and not self.greedy:
                matches = [ po for po in matches if po.arch in self.valid_multilib_arches ]

            if not matches:
                self.logger.warn('Could not find a match for %s in any configured repo' % pattern)
                continue

            packages_by_name = {}
            for po in matches:
                packages_by_name.setdefault(po.name, []).append(po)

            for name, packages in packages_by_name.iteritems():
                packages = self.excludePackages(packages)
                if self.greedy:
                    packages = yum.packageSack.ListPackageSack(packages).returnNewestByNameArch()
                else:
                    packages = [self.ayum._bestPackageFromList(packages)]

                for po in packages:
                    msg = 'Found %s.%s' % (po.name, po.arch)
                    self.add_package(po, msg)

        if len(self.ayum.tsInfo) == 0:
            raise yum.Errors.MiscError, 'No packages found to download.'

        moretoprocess = True
        while moretoprocess: # Our fun loop
            moretoprocess = False
            for txmbr in self.ayum.tsInfo:
                if not final_pkgobjs.has_key(txmbr.po):
                    final_pkgobjs[txmbr.po] = None # Add the pkg to our final list
                    self.getPackageDeps(txmbr.po) # Get the deps of our package
                    moretoprocess = True

        self.polist = final_pkgobjs.keys()
        self.logger.info('Finished gathering package objects.')

    def getSRPMPo(self, po):
        """Given a package object, get a package object for the
           corresponding source rpm. Requires yum still configured
           and a valid package object."""
        srpm = po.sourcerpm.split('.src.rpm')[0]
        (sname, sver, srel) = srpm.rsplit('-', 2)
        try:
            srpmpo = self.ayum.pkgSack.searchNevra(name=sname, ver=sver, rel=srel, arch='src')[0]
            return srpmpo
        except IndexError:
            print >> sys.stderr, "Error: Cannot find a source rpm for %s" % srpm
            sys.exit(1)

    def createSourceHashes(self):
        """Create two dicts - one that maps binary POs to source POs, and
           one that maps a single source PO to all binary POs it produces.
           Requires yum still configured."""
        self.src_by_bin = {}
        self.bin_by_src = {}
        self.logger.info("Generating source <-> binary package mappings")
        (dummy1, everything, dummy2) = yum.packages.parsePackages(self.pkgs, ['*'], pkgdict=self.pkg_refs.copy())
        for po in everything:
            if po.arch == 'src':
                continue
            srpmpo = self.getSRPMPo(po)
            self.src_by_bin[po] = srpmpo
            if self.bin_by_src.has_key(srpmpo):
                self.bin_by_src[srpmpo].append(po)
            else:
                self.bin_by_src[srpmpo] = [po]

    def getSRPMList(self):
        """Cycle through the list of package objects and
           find the sourcerpm for them.  Requires yum still
           configured and a list of package objects"""
        for po in self.polist[self.last_po:]:
            srpm_po = self.src_by_bin[po]
            if not srpm_po in self.srpmpolist:
                msg = "Adding source package %s.%s" % (srpm_po.name, srpm_po.arch)
                self.add_source(srpm_po)
        self.last_po = len(self.polist)

    def resolvePackageBuildDeps(self):
        """Make the package lists self hosting. Requires yum
           still configured, a list of package objects, and a
           a list of source rpms."""
        deppass = 1
        while 1:
            self.logger.info("Resolving build dependencies, pass %d" % (deppass))
            prev = list(self.ayum.tsInfo.getMembers())
            for srpm in self.srpmpolist[len(self.srpms_build):]:
                self.getPackageDeps(srpm)
            for txmbr in self.ayum.tsInfo:
                if txmbr.po.arch != 'src' and txmbr.po not in self.polist:
                    self.polist.append(txmbr.po)
                    self.getPackageDeps(txmbr.po)
            self.srpms_build = list(self.srpmpolist)
            # Now that we've resolved deps, refresh the source rpm list
            self.getSRPMList()
            deppass = deppass + 1
            if len(prev) == len(self.ayum.tsInfo.getMembers()):
                break

    def completePackageSet(self):
        """Cycle through all package objects, and add any
           that correspond to a source rpm that we are including.
           Requires yum still configured and a list of package
           objects."""
        thepass = 1
        while 1:
            prevlen = len(self.srpmpolist)
            self.logger.info("Completing package set, pass %d" % (thepass,))
            for srpm in self.srpmpolist[len(self.srpms_fulltree):]:

                include_native = False
                include_multilib = False
                has_native = False
                has_multilib = False
                for po in self.excludePackages(self.bin_by_src[srpm]):
                    if not is_package(po):
                        continue
                    if po.arch == "noarch":
                        continue
                    if po not in self.polist:
                        # process only already included packages
                        if po.arch in self.valid_multilib_arches:
                            has_multilib = True
                        elif po.arch in self.valid_native_arches:
                            has_native = True
                        continue
                    if po.arch in self.valid_multilib_arches:
                        include_multilib = True
                    elif po.arch in self.valid_native_arches:
                        include_native = True

                # XXX: this is very fragile!
                # Do not make any changes unless you really know what you're doing!
                if not include_native:
                    # if there's no native package already pulled in...
                    if has_native and not include_multilib:
                        # include all native packages, but only if we're not pulling multilib already
                        # SCENARIO: a noarch package was already pulled in and there are x86_64 and i686 packages -> we want x86_64 in to complete the package set
                        include_native = True
                    elif has_multilib:
                        # SCENARIO: a noarch package was already pulled in and there are no x86_64 packages; we want i686 in to complete the package set
                        include_multilib = True

                for po in self.excludePackages(self.bin_by_src[srpm]):
                    if not is_package(po):
                        continue
                    if po in self.polist:
                        continue
                    if po.arch != "noarch":
                        if po.arch in self.valid_multilib_arches:
                            if not include_multilib:
                                continue
                        if po.arch in self.valid_native_arches:
                            if not include_native:
                                continue
                    msg = "Adding %s.%s to complete package set" % (po.name, po.arch)
                    self.add_package(po, msg)
                    self.getPackageDeps(po)
            for txmbr in self.ayum.tsInfo:
                if txmbr.po.arch != 'src' and txmbr.po not in self.polist:
                    self.polist.append(txmbr.po)
                    self.getPackageDeps(po)
            self.srpms_fulltree = list(self.srpmpolist)
            # Now that we've resolved deps, refresh the source rpm list
            self.getSRPMList()
            if len(self.srpmpolist) == prevlen:
                self.logger.info("Completion finished in %d passes" % (thepass,))
                break
            thepass = thepass + 1


    def getDebuginfoList(self):
        """Cycle through the list of package objects and find
           debuginfo rpms for them.  Requires yum still
           configured and a list of package objects"""

        for po in self.pkgs:
            if not is_debug(po):
                continue

            if po.sourcerpm not in self.sourcerpm_arch_map:
                # TODO: print a warning / throw an error
                continue
            if not (set(self.compatible_arches[po.arch]) & set(self.sourcerpm_arch_map[po.sourcerpm]) - set(["noarch"])):
                # skip all incompatible arches
                # this pulls i386 debuginfo for a i686 package for example
                continue
            msg = 'Added debuginfo %s.%s' % (po.name, po.arch)
            self.add_debuginfo(po, msg)

    def _downloadPackageList(self, polist, relpkgdir):
        """Cycle through the list of package objects and
           download them from their respective repos."""

        downloads = []
        for pkg in polist:
            downloads.append('%s.%s' % (pkg.name, pkg.arch))
            downloads.sort()
        self.logger.info("Download list: %s" % downloads)

        pkgdir = os.path.join(self.config.get('pungi', 'destdir'),
                              self.config.get('pungi', 'version'),
                              self.config.get('pungi', 'flavor'),
                              relpkgdir)

        # Ensure the pkgdir exists, force if requested, and make sure we clean it out
        if relpkgdir.endswith('SRPMS'):
            # Since we share source dirs with other arches don't clean, but do allow us to use it
            pypungi.util._ensuredir(pkgdir, self.logger, force=True, clean=False)
        else:
            pypungi.util._ensuredir(pkgdir, self.logger, force=self.config.getboolean('pungi', 'force'), clean=True)

        probs = self.ayum.downloadPkgs(polist)

        if len(probs.keys()) > 0:
            self.logger.error("Errors were encountered while downloading packages.")
            for key in probs.keys():
                errors = yum.misc.unique(probs[key])
                for error in errors:
                    self.logger.error("%s: %s" % (key, error))
            sys.exit(1)

        for po in polist:
            basename = os.path.basename(po.relativepath)

            local = po.localPkg()
            if self.config.getboolean('pungi', 'nohash'):
                target = os.path.join(pkgdir, basename)
            else:
                target = os.path.join(pkgdir, po.name[0].lower(), basename)
                # Make sure we have the hashed dir available to link into we only want dirs there to corrospond to packages
                # that we are including so we can not just do A-Z 0-9
                pypungi.util._ensuredir(os.path.join(pkgdir, po.name[0].lower()), self.logger, force=True, clean=False)

            # Link downloaded package in (or link package from file repo)
            try:
                pypungi.util._link(local, target, self.logger, force=True)
                continue
            except:
                self.logger.error("Unable to link %s from the yum cache." % po.name)
                sys.exit(1)

        self.logger.info('Finished downloading packages.')

    def downloadPackages(self):
        """Download the package objects obtained in getPackageObjects()."""

        self._downloadPackageList(self.polist,
                                  os.path.join(self.tree_arch,
                                               self.config.get('pungi', 'osdir'),
                                               self.config.get('pungi', 'product_path')))

    def makeCompsFile(self):
        """Gather any comps files we can from repos and merge them into one."""

        ourcompspath = os.path.join(self.workdir, '%s-%s-comps.xml' % (self.config.get('pungi', 'name'), self.config.get('pungi', 'version')))

        # Filter out things we don't include
        ourgroups = []
        for item in self.ksparser.handler.packages.groupList:
            g = self.ayum.comps.return_group(item.name)
            if g:
                ourgroups.append(g.groupid)
        allgroups = [g.groupid for g in self.ayum.comps.get_groups()]
        for group in allgroups:
            if group not in ourgroups and not self.ayum.comps.return_group(group).langonly:
                self.logger.info('Removing extra group %s from comps file' % (group,))
                del self.ayum.comps._groups[group]

        groups = [g.groupid for g in self.ayum.comps.get_groups()]
        envs = self.ayum.comps.get_environments()
        for env in envs:
            for group in env.groups:
                if group not in groups:
                    self.logger.info('Removing incomplete environment %s from comps file' % (env,))
                    del self.ayum.comps._environments[env.environmentid]
                    break

        ourcomps = open(ourcompspath, 'w')
        ourcomps.write(self.ayum.comps.xml())
        ourcomps.close()

        # Disable this until https://bugzilla.redhat.com/show_bug.cgi?id=442097 is fixed.
        # Run the xslt filter over our comps file
        #compsfilter = ['/usr/bin/xsltproc', '--novalid']
        #compsfilter.append('-o')
        #compsfilter.append(ourcompspath)
        #compsfilter.append('/usr/share/pungi/comps-cleanup.xsl')
        #compsfilter.append(ourcompspath)

        #pypungi.util._doRunCommand(compsfilter, self.logger)

    def downloadSRPMs(self):
        """Cycle through the list of srpms and
           find the package objects for them, Then download them."""

        # do the downloads
        self._downloadPackageList(self.srpmpolist, os.path.join('source', 'SRPMS'))

    def downloadDebuginfo(self):
        """Cycle through the list of debuginfo rpms and
           download them."""

        # do the downloads
        self._downloadPackageList(self.debuginfolist, os.path.join(self.tree_arch, 'debug'))

    def _listPackages(self, polist):
        """Cycle through the list of packages and return their paths."""
        return [ os.path.join(pkg.basepath or "", pkg.relativepath) for pkg in polist if pkg.repoid not in self.lookaside_repos ]

    def listPackages(self):
        """Cycle through the list of RPMs and return their paths."""
        return self._listPackages(self.polist)

    def listSRPMs(self):
        """Cycle through the list of SRPMs and return their paths."""
        return self._listPackages(self.srpmpolist)

    def listDebuginfo(self):
        """Cycle through the list of DEBUGINFO RPMs and return their paths."""
        return self._listPackages(self.debuginfolist)

    def writeinfo(self, line):
        """Append a line to the infofile in self.infofile"""


        f=open(self.infofile, "a+")
        f.write(line.strip() + "\n")
        f.close()

    def mkrelative(self, subfile):
        """Return the relative path for 'subfile' underneath the version dir."""

        basedir = os.path.join(self.destdir, self.config.get('pungi', 'version'))
        if subfile.startswith(basedir):
            return subfile.replace(basedir + os.path.sep, '')
        
    def _makeMetadata(self, path, cachedir, comps=False, repoview=False, repoviewtitle=False,
                      baseurl=False, output=False, basedir=False, update=True):
        """Create repodata and repoview."""
        
        conf = createrepo.MetaDataConfig()
        conf.cachedir = os.path.join(cachedir, 'createrepocache')
        conf.update = update
        conf.unique_md_filenames = True
        if output:
            conf.outputdir = output
        else:
            conf.outputdir = path
        conf.directory = path
        conf.database = True
        if comps:
           conf.groupfile = comps
        if basedir:
            conf.basedir = basedir
        if baseurl:
            conf.baseurl = baseurl
        repomatic = createrepo.MetaDataGenerator(conf)
        self.logger.info('Making repodata')
        repomatic.doPkgMetadata()
        repomatic.doRepoMetadata()
        repomatic.doFinalMove()
        
        if repoview:
            # setup the repoview call
            repoview = ['/usr/bin/repoview']
            repoview.append('--quiet')
            
            repoview.append('--state-dir')
            repoview.append(os.path.join(cachedir, 'repoviewcache'))
            
            if repoviewtitle:
                repoview.append('--title')
                repoview.append(repoviewtitle)
    
            repoview.append(path)
    
            # run the command
            pypungi.util._doRunCommand(repoview, self.logger)
        
    def doCreaterepo(self, comps=True):
        """Run createrepo to generate repodata in the tree."""


        compsfile = None
        if comps:
            compsfile = os.path.join(self.workdir, '%s-%s-comps.xml' % (self.config.get('pungi', 'name'), self.config.get('pungi', 'version')))
        
        # setup the cache dirs
        for target in ['createrepocache', 'repoviewcache']:
            pypungi.util._ensuredir(os.path.join(self.config.get('pungi', 'cachedir'),
                                            target), 
                               self.logger, 
                               force=True)
            
        repoviewtitle = '%s %s - %s' % (self.config.get('pungi', 'name'), 
                                        self.config.get('pungi', 'version'),
                                        self.tree_arch)

        cachedir = self.config.get('pungi', 'cachedir')

        # setup the createrepo call
        self._makeMetadata(self.topdir, cachedir, compsfile, repoview=True, repoviewtitle=repoviewtitle)

        # create repodata for debuginfo
        if self.config.getboolean('pungi', 'debuginfo'):
            path = os.path.join(self.archdir, 'debug')
            if not os.path.isdir(path):
                self.logger.debug("No debuginfo for %s" % self.tree_arch)
                return
            self._makeMetadata(path, cachedir, repoview=False)

    def doBuildinstall(self):
        """Run lorax on the tree."""

        # the old ayum object has transaction data that confuse lorax, reinit.
        self._inityum()

        # Add the repo in the destdir to our yum object
        self._add_yum_repo('ourtree',
                           'file://%s' % self.topdir,
                           cost=10)

        product = self.config.get('pungi', 'name')
        version = self.config.get('pungi', 'version')
        release = '%s %s' % (self.config.get('pungi', 'name'), self.config.get('pungi', 'version'))

        variant = self.config.get('pungi', 'flavor')
        bugurl = self.config.get('pungi', 'bugurl')
        isfinal = self.config.get('pungi', 'isfinal')

        workdir = self.workdir
        outputdir = self.topdir

        # on ppc64 we need to tell lorax to only use ppc64 packages so that the media will run on all 64 bit ppc boxes
        if self.config.get('pungi', 'arch') == 'ppc64':
            self.ayum.arch.setup_arch('ppc64')
            self.ayum.compatarch = 'ppc64'

        # Only supported mac hardware is x86 make sure we only enable mac support on arches that need it
        if self.config.get('pungi', 'arch') in ['i386', 'i686', 'x86_64']:
            domacboot = True
        else:
            domacboot = False

        # run the command
        lorax = pylorax.Lorax()
        lorax.configure()

        lorax.run(self.ayum, product=product, version=version, release=release,
                  variant=variant, bugurl=bugurl, isfinal=isfinal, domacboot=domacboot,
                  workdir=workdir, outputdir=outputdir)

        # write out the tree data for snake
        self.writeinfo('tree: %s' % self.mkrelative(self.topdir))

        # Write out checksums for verifytree
        # First open the treeinfo file so that we can config parse it
        treeinfofile = os.path.join(self.topdir, '.treeinfo')

        try:
            treefile = open(treeinfofile, 'r')
        except IOError:
            self.logger.error("Could not read .treeinfo file: %s" % treefile)
            sys.exit(1)

        # Create a ConfigParser object out of the contents so that we can
        # write it back out later and not worry about formatting
        treeinfo = MyConfigParser()
        treeinfo.readfp(treefile)
        treefile.close()
        treeinfo.add_section('checksums')

        # Create a function to use with os.path.walk to sum the files
        # basepath is used to make the sum output relative
        sums = []
        def getsum(basepath, dir, files):
            for file in files:
                path = os.path.join(dir, file)
                # don't bother summing directories.  Won't work.
                if os.path.isdir(path):
                    continue
                sum = pypungi.util._doCheckSum(path, 'sha256', self.logger)
                outpath = path.replace(basepath, '')
                sums.append((outpath, sum))

        # Walk the os/images path to get sums of all the files
        os.path.walk(os.path.join(self.topdir, 'images'), getsum, self.topdir + '/')
        
        # Capture PPC images
        if self.tree_arch in ['ppc', 'ppc64']:
            os.path.walk(os.path.join(self.topdir, 'ppc'), getsum, self.topdir + '/')

        # Get a checksum of repomd.xml since it has within it sums for other files
        repomd = os.path.join(self.topdir, 'repodata', 'repomd.xml')
        sum = pypungi.util._doCheckSum(repomd, 'sha256', self.logger)
        sums.append((os.path.join('repodata', 'repomd.xml'), sum))

        # Now add the sums, and write the config out
        try:
            treefile = open(treeinfofile, 'w')
        except IOError:
            self.logger.error("Could not open .treeinfo for writing: %s" % treefile)
            sys.exit(1)

        for path, sum in sums:
            treeinfo.set('checksums', path, sum)

        treeinfo.write(treefile)
        treefile.close()

    def doGetRelnotes(self):
        """Get extra files from packages in the tree to put in the topdir of
           the tree."""


        docsdir = os.path.join(self.workdir, 'docs')
        relnoterpms = self.config.get('pungi', 'relnotepkgs').split()

        fileres = []
        for pattern in self.config.get('pungi', 'relnotefilere').split():
            fileres.append(re.compile(pattern))

        dirres = []
        for pattern in self.config.get('pungi', 'relnotedirre').split():
            dirres.append(re.compile(pattern))

        pypungi.util._ensuredir(docsdir, self.logger, force=self.config.getboolean('pungi', 'force'), clean=True)

        # Expload the packages we list as relnote packages
        pkgs = os.listdir(os.path.join(self.topdir, self.config.get('pungi', 'product_path')))

        rpm2cpio = ['/usr/bin/rpm2cpio']
        cpio = ['cpio', '-imud']

        for pkg in pkgs:
            pkgname = pkg.rsplit('-', 2)[0]
            for relnoterpm in relnoterpms:
                if pkgname == relnoterpm:
                    extraargs = [os.path.join(self.topdir, self.config.get('pungi', 'product_path'), pkg)]
                    try:
                        p1 = subprocess.Popen(rpm2cpio + extraargs, cwd=docsdir, stdout=subprocess.PIPE)
                        (out, err) = subprocess.Popen(cpio, cwd=docsdir, stdin=p1.stdout, stdout=subprocess.PIPE, 
                            stderr=subprocess.PIPE, universal_newlines=True).communicate()
                    except:
                        self.logger.error("Got an error from rpm2cpio")
                        self.logger.error(err)
                        raise

                    if out:
                        self.logger.debug(out)

        # Walk the tree for our files
        for dirpath, dirname, filelist in os.walk(docsdir):
            for filename in filelist:
                for regex in fileres:
                    if regex.match(filename) and not os.path.exists(os.path.join(self.topdir, filename)):
                        self.logger.info("Linking release note file %s" % filename)
                        pypungi.util._link(os.path.join(dirpath, filename),
                                           os.path.join(self.topdir, filename),
                                           self.logger,
                                           force=self.config.getboolean('pungi',
                                                                        'force'))
                        self.common_files.append(filename)

        # Walk the tree for our dirs
        for dirpath, dirname, filelist in os.walk(docsdir):
            for directory in dirname:
                for regex in dirres:
                    if regex.match(directory) and not os.path.exists(os.path.join(self.topdir, directory)):
                        self.logger.info("Copying release note dir %s" % directory)
                        shutil.copytree(os.path.join(dirpath, directory), os.path.join(self.topdir, directory))
        
    def _doIsoChecksum(self, path, csumfile):
        """Simple function to wrap creating checksums of iso files."""

        try:
            checkfile = open(csumfile, 'a')
        except IOError:
            self.logger.error("Could not open checksum file: %s" % csumfile)

        self.logger.info("Generating checksum of %s" % path)
        checksum = pypungi.util._doCheckSum(path, 'sha256', self.logger)
        if checksum:
            checkfile.write("%s *%s\n" % (checksum.replace('sha256:', ''), os.path.basename(path)))
        else:
            self.logger.error('Failed to generate checksum for %s' % checkfile)
            sys.exit(1)
        checkfile.close()

    def doCreateIsos(self):
        """Create iso of the tree."""

        if self.config.get('pungi', 'arch').startswith('arm'):
            self.logger.info("ARCH: arm, not doing doCreateIsos().")
            return

        isolist=[]
        ppcbootinfo = '/usr/share/lorax/config_files/ppc'

        pypungi.util._ensuredir(self.isodir, self.logger,
                           force=self.config.getboolean('pungi', 'force'),
                           clean=True) # This is risky...

        # setup the base command
        mkisofs = ['/usr/bin/mkisofs']
        mkisofs.extend(['-v', '-U', '-J', '-R', '-T', '-m', 'repoview', '-m', 'boot.iso']) # common mkisofs flags

        x86bootargs = ['-b', 'isolinux/isolinux.bin', '-c', 'isolinux/boot.cat', 
            '-no-emul-boot', '-boot-load-size', '4', '-boot-info-table']

        efibootargs = ['-eltorito-alt-boot', '-e', 'images/efiboot.img',
                       '-no-emul-boot']

        macbootargs = ['-eltorito-alt-boot', '-e', 'images/macboot.img',
                       '-no-emul-boot']

        ia64bootargs = ['-b', 'images/boot.img', '-no-emul-boot']

        ppcbootargs = ['-part', '-hfs', '-r', '-l', '-sysid', 'PPC', '-no-desktop', '-allow-multidot', '-chrp-boot']

        ppcbootargs.append('-map')
        ppcbootargs.append(os.path.join(ppcbootinfo, 'mapping'))

        ppcbootargs.append('-magic')
        ppcbootargs.append(os.path.join(ppcbootinfo, 'magic'))

        ppcbootargs.append('-hfs-bless') # must be last

        isohybrid = ['/usr/bin/isohybrid']

        # Check the size of the tree
        # This size checking method may be bunk, accepting patches...
        if not self.tree_arch == 'source':
            treesize = int(subprocess.Popen(mkisofs + ['-print-size', '-quiet', self.topdir], stdout=subprocess.PIPE).communicate()[0])
        else:
            srcdir = os.path.join(self.config.get('pungi', 'destdir'), self.config.get('pungi', 'version'), 
                                  self.config.get('pungi', 'flavor'), 'source', 'SRPMS')

            treesize = int(subprocess.Popen(mkisofs + ['-print-size', '-quiet', srcdir], stdout=subprocess.PIPE).communicate()[0])
        # Size returned is 2KiB clusters or some such.  This translates that to MiB.
        treesize = treesize * 2048 / 1024 / 1024

        if treesize > 700: # we're larger than a 700meg CD
            isoname = '%s-%s-%s-DVD.iso' % (self.config.get('pungi', 'iso_basename'), self.config.get('pungi', 'version'), 
                self.tree_arch)
        else:
            isoname = '%s-%s-%s.iso' % (self.config.get('pungi', 'iso_basename'), self.config.get('pungi', 'version'), 
                self.tree_arch)

        isofile = os.path.join(self.isodir, isoname)

        # setup the extra mkisofs args
        extraargs = []

        if self.tree_arch == 'i386' or self.tree_arch == 'x86_64':
            extraargs.extend(x86bootargs)
            if self.tree_arch == 'x86_64':
                extraargs.extend(efibootargs)
                isohybrid.append('-u')
                if os.path.exists(os.path.join(self.topdir, 'images', 'macboot.img')):
                    extraargs.extend(macbootargs)
                    isohybrid.append('-m')
        elif self.tree_arch == 'ia64':
            extraargs.extend(ia64bootargs)
        elif self.tree_arch.startswith('ppc'):
            extraargs.extend(ppcbootargs)
            extraargs.append(os.path.join(self.topdir, "ppc/mac"))

        # NOTE: if this doesn't match what's in the bootloader config, the
        # image won't be bootable!
        extraargs.append('-V')
        extraargs.append('%s %s %s' % (self.config.get('pungi', 'name'),
            self.config.get('pungi', 'version'), self.tree_arch))

        extraargs.extend(['-o', isofile])

        isohybrid.append(isofile)

        if not self.tree_arch == 'source':
            extraargs.append(self.topdir)
        else:
            extraargs.append(os.path.join(self.archdir, 'SRPMS'))

        # run the command
        pypungi.util._doRunCommand(mkisofs + extraargs, self.logger)

        # Run isohybrid on the iso as long as its not the source iso
        if os.path.exists("/usr/bin/isohybrid") and not self.tree_arch == 'source':
            pypungi.util._doRunCommand(isohybrid, self.logger)

        # implant md5 for mediacheck on all but source arches
        if not self.tree_arch == 'source':
            pypungi.util._doRunCommand(['/usr/bin/implantisomd5', isofile], self.logger)

        # shove the checksum into a file
        csumfile = os.path.join(self.isodir, '%s-%s-%s-CHECKSUM' % (
                                self.config.get('pungi', 'iso_basename'),
                                self.config.get('pungi', 'version'),
                                self.tree_arch))
        # Write a line about what checksums are used.
        # sha256sum is magic...
        file = open(csumfile, 'w')
        file.write('# The image checksum(s) are generated with sha256sum.\n')
        file.close()
        self._doIsoChecksum(isofile, csumfile)

        # Write out a line describing the media
        self.writeinfo('media: %s' % self.mkrelative(isofile))

        # Now link the boot iso
        if not self.tree_arch == 'source' and \
        os.path.exists(os.path.join(self.topdir, 'images', 'boot.iso')):
            isoname = '%s-%s-%s-netinst.iso' % (self.config.get('pungi', 'iso_basename'),
                self.config.get('pungi', 'version'), self.tree_arch)
            isofile = os.path.join(self.isodir, isoname)

            # link the boot iso to the iso dir
            pypungi.util._link(os.path.join(self.topdir, 'images', 'boot.iso'), isofile, self.logger)

            # shove the checksum into a file
            self._doIsoChecksum(isofile, csumfile)

        self.logger.info("CreateIsos is done.")
