# -*- coding: utf-8 -*-
"""转存链接/Excel 解析的回归测试(曾有"访问码被当标题吃掉"导致两行格式 100% 失败)"""
import io
import os
import sys
import unittest

os.environ["TREE_DB"] = "/tmp/t_parse.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transfer_worker as t   # noqa: E402


class TestParseText(unittest.TestCase):
    def test_link_plus_pw_only(self):
        # 核心回归: 链接+访问码(无标题) —— 旧版把访问码当标题吃掉
        r = t.parse_text("https://115.com/s/sw6vxnqmd1k\n访问码：ab12")[0]
        self.assertEqual(r["receive_code"], "ab12")
        self.assertEqual(r["title"], "")

    def test_link_title_pw(self):
        r = t.parse_text("https://115.com/s/sw6vxnqmd1k\n复仇者联盟4\n访问码：ab12")[0]
        self.assertEqual((r["receive_code"], r["title"]), ("ab12", "复仇者联盟4"))

    def test_title_inline(self):
        r = t.parse_text("2012 (2009) https://115.com/s/abc123xyz90")[0]
        self.assertEqual((r["receive_code"], r["title"]), ("", "2012 (2009)"))

    def test_url_password(self):
        r = t.parse_text("https://115.com/s/sw6vxnqmd1k?password=ab12#xxx")[0]
        self.assertEqual((r["receive_code"], r["title"]), ("ab12", ""))

    def test_multi_entries(self):
        r = t.parse_text("标题甲 https://115.com/s/aaa111bbb2\n标题乙 https://115.com/s/ccc111ddd2")
        self.assertEqual([x["title"] for x in r], ["标题甲", "标题乙"])

    def test_halfwidth_colon(self):
        r = t.parse_text("https://115.com/s/sw6vxnqmd1k\n访问码:xy99")[0]
        self.assertEqual(r["receive_code"], "xy99")


class TestParseXlsx(unittest.TestCase):
    def _parse(self, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return t.parse_upload("list.xlsx", buf.getvalue())

    def test_numeric_title_not_eaten_as_pw(self):
        # "2012" 这类纯数字片名曾被误当访问码吃掉
        items = self._parse([["https://115.com/s/aaa111bbb2", "2012"]])
        self.assertEqual((items[0]["receive_code"], items[0]["title"]), ("", "2012"))

    def test_title_plus_pw(self):
        items = self._parse([["https://115.com/s/ccc111ddd2", "复仇者联盟4", "ab12"]])
        self.assertEqual((items[0]["receive_code"], items[0]["title"]), ("ab12", "复仇者联盟4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
