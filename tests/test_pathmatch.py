#!/usr/bin/python
# -*- coding: utf-8 -*-


import unittest
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "pypungi")))

from pathmatch import PathMatch, head_tail_split


class TestHeadTailSplit(unittest.TestCase):
    def test_1(self):
        head, tail = head_tail_split("a")
        self.assertEqual(head, "a")
        self.assertEqual(tail, None)

        head, tail = head_tail_split("/*")
        self.assertEqual(head, "*")
        self.assertEqual(tail, None)

        head, tail = head_tail_split("///*")
        self.assertEqual(head, "*")
        self.assertEqual(tail, None)

        head, tail = head_tail_split("///*//")
        self.assertEqual(head, "*")
        self.assertEqual(tail, None)

        head, tail = head_tail_split("///*//-")
        self.assertEqual(head, "*")
        self.assertEqual(tail, "-")


class TestPathMatch(unittest.TestCase):

    def setUp(self):
        self.pm = PathMatch()

    def test_1(self):
        self.pm["/*"] = "/star1"
        self.assertEqual(self.pm._final_patterns.keys(), ["*"])
        self.assertEqual(self.pm._values, [])
        self.assertEqual(self.pm._final_patterns["*"]._values, ["/star1"])
        self.assertEqual(sorted(self.pm["/lib"]), ["/star1"])

        self.pm["/*"] = "/star2"
        self.assertEqual(sorted(self.pm["/lib"]), ["/star1", "/star2"])

        self.pm["/lib"] = "/lib"
        self.assertEqual(sorted(self.pm["/lib"]), ["/lib", "/star1", "/star2"])

        self.pm["/lib64"] = "/lib64"
        self.assertEqual(sorted(self.pm["/lib64"]), ["/lib64", "/star1", "/star2"])

    def test_2(self):
        self.pm["/*/*"] = "/star/star1"
        self.assertEqual(self.pm._patterns.keys(), ["*"])
        self.assertEqual(self.pm._patterns["*"]._final_patterns.keys(), ["*"])
        self.assertEqual(self.pm._patterns["*"]._final_patterns["*"]._values, ["/star/star1"])
        self.assertEqual(sorted(self.pm["/lib/asd"]), ["/star/star1"])

        self.pm["/*"] = "/star2"
        self.assertEqual(sorted(self.pm["/lib"]), ["/star2"])

        self.assertEqual(sorted(self.pm["/lib/foo"]), ["/star/star1", "/star2"])


if __name__ == "__main__":
    unittest.main()
