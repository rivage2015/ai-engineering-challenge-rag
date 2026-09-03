#!/usr/bin/env python3
"""Build a deterministic pre-answer graph for structured questions.

The semantic reader has already converted source files into immutable Evidence
text records.  This module does not OCR, execute formulas, or ask an LLM to
guess a value.  It connects count/total questions to explicit aggregate
Evidence and planned record lookups to verified raw header/value lineage, then
emits a hash-bound Graph Artifact that can be revalidated before final audit.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import deque
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


GRAPH_VERSION = "1.3"
VALIDATOR_VERSION = "1.3"
STORED_GRAPH_BINDING_VERSION = "1.0"
PROVISIONAL_MARKER = "[暫定読取]"
COUNT_SURFACES = (
    "何回", "回数", "何枠", "枠数", "何件", "件数", "総数", "合計",
)
STRONG_COUNT_SURFACES = (
    "何回", "回数", "何枠", "枠数", "何件", "件数", "総数",
)
GENERIC_QUERY_TERMS = (
    "何回", "回数", "何枠", "枠数", "何件", "件数", "総数", "合計", "です", "ます",
    "でした", "か", "における", "について", "記録", "業務", "稼働", "出勤",
)
FIELD_LINE = re.compile(r"^(?P<label>[^:\n]{1,100}):\s*(?P<value>.*)$")
SUM_FORMULA = re.compile(
    r"=\s*SUM\(\$?(?P<start_col>[A-Z]{1,3})\$?(?P<start_row>\d+)"
    r":\$?(?P<end_col>[A-Z]{1,3})\$?(?P<end_row>\d+)\)",
    re.IGNORECASE,
)
SAVED_VALUE = re.compile(
    r"\[保存値[^]:]*:\s*(?P<value>[-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*\]"
)
SAVED_VALUE_ANNOTATION = re.compile(
    r"\s+\[保存値[^\]]*[:：]\s*"
    r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)\s*\]\s*$"
)
CELL_REFERENCE = re.compile(
    r"=\s*(?:(?:'(?P<quoted_sheet>[^']+)'|(?P<sheet>[^!\s=\[]+))!)?"
    r"\$?(?P<column>[A-Z]{1,3})\$?(?P<row>\d+)",
    re.IGNORECASE,
)
CELL_COORDINATE = re.compile(r"^(?P<column>[A-Z]{1,3})(?P<row>\d+)$", re.IGNORECASE)
YEAR_MONTH = re.compile(r"(?P<year>20\d{2})\s*年\s*(?P<month>1[0-2]|0?[1-9])\s*月")
FINAL_RECORD_SURFACE = re.compile(
    r"(?<![0-9A-Za-z])(?:finalized|final|approved)(?![0-9A-Za-z])"
    r"|最終(?:版|確定)?|承認済み",
    re.IGNORECASE,
)
NEGATED_FINAL_RECORD_SURFACE = re.compile(
    r"(?<![0-9A-Za-z])(?:"
    r"not(?:\s+yet)?(?:\s+been)?"
    r"|(?:isn['’]t|aren['’]t|wasn['’]t|weren['’]t|"
    r"hasn['’]t|haven['’]t|hadn['’]t)(?:\s+been)?"
    r")\s+(?:finalized|final|approved)(?![0-9A-Za-z])"
    r"|(?:最終(?:版|確定)?|承認済み)\s*"
    r"(?:ではない|ではありません|ではございません|"
    r"でない|じゃない|じゃありません|"
    r"ではなく|でなく|ではなかった)",
    re.IGNORECASE,
)
NONFINAL_RECORD_SURFACE = re.compile(
    r"(?<![0-9A-Za-z])(?:draft|old|superseded)(?![0-9A-Za-z])"
    r"|ドラフト|下書き|旧版|廃止済み",
    re.IGNORECASE,
)
RECORD_LOOKUP_FIELD_ALIASES = {
    "owner": (
        "owner", "assignee", "person in charge",
        "担当者", "担当者名", "担当者氏名", "担当", "主担当", "担当社員", "責任者",
        "担当氏名", "責任者名", "assignee name", "受け持",
        "副担当", "副担当者", "共同担当", "co-owner", "secondary owner",
    ),
    "review_date": (
        "review date", "review_date", "reviewdate", "レビュー日", "確認日",
    ),
    "unit_cost": ("unit cost", "unit_cost", "unitcost", "単価"),
    "seats": (
        "seat count", "seat_count", "seatcount", "seats", "席数", "座席数",
    ),
    "budget": (
        "budget calculation", "budget_calculation", "budgetcalculation",
        "budget", "予算計算", "予算",
    ),
}
RECORD_STATUS_LABEL_ALIASES = (
    "status", "record status", "version status", "state", "ステータス", "状態", "版状態",
)
RECORD_SUBJECT_LABEL_ALIASES = (
    "project", "project name", "subject", "name", "title", "initiative",
    "プロジェクト", "案件", "件名", "対象", "名称", "業務", "業務名", "作業", "タスク",
)
TEMPORAL_RECORD_SUBJECT_LABEL_ALIASES = (
    "project", "project name", "project title", "initiative", "assignment",
    "assignment name", "assignment subject", "assignment target",
    "assigned project", "project target", "work target", "task target",
    "work item", "work name", "task name",
    "プロジェクト", "プロジェクト名", "プロジェクト名称", "プロジェクトの名称",
    "案件", "案件名", "案件名称", "案件の名称",
    "業務", "業務名", "業務名称", "担当業務", "担当案件",
    "対象業務", "対象業務名", "対象業務名称", "作業", "作業名", "対象作業",
    "作業名称", "作業の名称", "タスク", "タスク名", "タスクの名称",
    "対象タスク", "対象プロジェクト",
    "対象名", "対象名称", "担当対象", "業務の名称",
)
FINAL_RECORD_STATUSES = (
    "approved", "final", "finalized", "承認済み", "最終", "最終版", "最終確定",
)
NONFINAL_RECORD_STATUS_MARKERS = ("draft", "old", "superseded")
TEMPORAL_START_LABEL_ALIASES = (
    "assignment start date", "assignee start date",
    "担当開始日", "担当開始年月日", "担当期間開始日", "着任日",
)
TEMPORAL_END_LABEL_ALIASES = (
    "assignment end date", "assignee end date",
    "担当終了日", "担当終了年月日", "担当期間終了日", "離任日",
)
POSSIBLE_PERIOD_START_LABEL_ALIASES = (
    *TEMPORAL_START_LABEL_ALIASES,
    "from", "start", "start date", "effective date",
    "effective start date", "valid from",
    "開始日", "開始年月日", "有効開始日", "有効開始年月日", "就任日",
    "担当期間開始", "期間開始日",
)
POSSIBLE_PERIOD_END_LABEL_ALIASES = (
    *TEMPORAL_END_LABEL_ALIASES,
    "to", "end", "end date", "expiration date",
    "effective end date", "valid to",
    "終了日", "終了年月日", "有効終了日", "有効終了年月日", "退任日",
    "担当期間終了", "期間終了日",
)
POSSIBLE_COMBINED_PERIOD_LABEL_ALIASES = (
    "assignment period", "assignee period", "validity period",
    "担当期間", "在任期間", "任期", "有効期間",
)
ASSIGNMENT_STATUS_LABEL_ALIASES = (
    "assignment status", "assignee status", "assignment active",
    "担当状態", "担当ステータス", "アサイン状態", "割当状態", "割り当て状態",
    "担当有効", "アサイン有効", "割当有効",
)
RELATIVE_YEAR_SURFACE = re.compile(
    r"(?<![0-9.+\-−〇零一二三四五六七八九十])"
    r"(?P<expression>(?P<count>[0-9]+|[〇零一二三四五六七八九十]+)\s*年前)"
)
RELATIVE_YEAR_COUNT_SURFACE = r"(?:[0-9]+|[〇零一二三四五六七八九十]+)"
RELATIVE_YEAR_APPROX_OR_RANGE = re.compile(
    rf"(?:約|およそ|だいたい|概ね|ほぼ|少なくとも|少なくても|"
    rf"最低でも|最大でも|遅くとも|早くとも)\s*"
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*年前"
    rf"|{RELATIVE_YEAR_COUNT_SURFACE}\s*年前\s*"
    rf"(?:から|以降|まで|以前|より後|より前|ごろ|頃|くらい|程度|あたり)"
    rf"|{RELATIVE_YEAR_COUNT_SURFACE}\s*年前後"
)
RELATIVE_YEAR_BOUND_SURFACE = re.compile(
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*年\s*"
    r"(?:以上|以下|未満|超|より)\s*前"
)
RELATIVE_YEAR_LIKE_SURFACE = re.compile(
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*年\s*"
    rf"(?:ほど|くらい|ぐらい|程度)\s*前"
)
RELATIVE_YEAR_BROAD_SURFACE = re.compile(
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*年[^。?？!！]{{0,12}}?前"
)
UNPARSED_YEARS_AGO_SURFACE = re.compile(
    r"年[^。?？!！\n]{0,8}?前"
)
NONCANONICAL_ASSIGNMENT_TIME_SURFACE = re.compile(
    r"(?:一昨日|昨日|今日|明日|明後日|先日|当日|前日|翌日|"
    r"先々週|先週|今週|来週|再来週|"
    r"先々月|先月|今月|来月|再来月|"
    r"一昨年度|昨年度|今年度|来年度|再来年度|"
    r"一昨年|昨年|去年|今年|来年|再来年|"
    r"現在|現時点|当時|その時|将来|今後|昔|過去|以前|以降|"
    r"前任|後任)"
    r"|(?:[0-9〇零一二三四五六七八九十百千万]+|数|半)\s*"
    r"(?:年|ねん|か月|ヶ月|ヵ月|ケ月|月|週間?|日間?|時間|分|秒)\s*"
    r"(?:前|まえ|後|あと)"
    r"|(?:開始|終了|交代|交替|異動|退任|着任|入社|退社|就任|離任)"
    r"(?:の)?(?:前|後|時点)"
    r"|今(?=\s*(?:の|は|で|、|,|時点|現在))"
)
TARGET_ABSOLUTE_TIME_MODIFIER_SURFACE = re.compile(
    rf"(?:[12]\d{{3}}\s*年\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*月"
    rf"(?:\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*日)?|"
    rf"(?:令和|平成|昭和)\s*(?:元|{RELATIVE_YEAR_COUNT_SURFACE})\s*年"
    rf"(?:\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*月)?|"
    rf"[12]\d{{3}}\s*年|{RELATIVE_YEAR_COUNT_SURFACE}\s*月)"
)
TARGET_EVENT_TIME_MODIFIER_SURFACE = re.compile(
    r"(?:開始|終了|退任|着任|引き継ぎ|引継ぎ|交代|交替|異動|入社|退社|就任|離任)"
    r"(?:時|時点|前|後)"
)
OTHER_ASSIGNMENT_TIME_SURFACE = re.compile(
    rf"過去\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*年(?:間)?"
    r"|(?:昨年度|一昨年度|昨年|去年|一昨年|先月|先週)"
    r"(?=\s*(?:の|は|に|で|、|,|時点|$))"
)
NON_YEAR_RELATIVE_TIME_SURFACE = re.compile(
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*"
    r"(?:か月|ヶ月|ヵ月|月|週|日)\s*前"
)
ABSOLUTE_DATE_SURFACE = re.compile(
    r"(?<!\d)[12]\d{3}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}(?!\d)"
)
GENERIC_ASSIGNMENT_TIME_SURFACE = re.compile(
    r"(?:時点|当時|その時|現在|現時点|過去|以前|以降|昔)"
)
ABSOLUTE_YEAR_SURFACE = re.compile(
    r"(?<!\d)[12]\d{3}\s*年"
    rf"|(?:令和|平成|昭和)\s*(?:元|{RELATIVE_YEAR_COUNT_SURFACE})\s*年"
)
CURRENT_TIME_SURFACE = re.compile(
    r"(?:現在|現時点|今)(?=\s*(?:の|は|と|も|を|まで|担当|誰|、|,|$))"
)
EXPLICIT_CURRENT_RELATIVE_ANCHOR = re.compile(
    rf"(?:現在|現時点|今)から\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*年前"
)
RELATIVE_YEAR_CALENDAR_DETAIL = re.compile(
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*年前\s*"
    r"(?:の\s*)?(?:時点(?:で|の)?\s*)?(?:は\s*)?(?:、|,)?\s*"
    rf"(?:{RELATIVE_YEAR_COUNT_SURFACE}\s*月"
    rf"(?:\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*日)?"
    r"(?:初|末|上旬|中旬|下旬)?|"
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*日|"
    rf"第?\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*週|"
    rf"年度(?:初|末|上期|下期)?|"
    rf"第?\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*四半期|"
    r"上期|下期|上半期|下半期|"
    r"月初|月末|年初|年末|年末年始|"
    r"元日|正月|同日|同月|同時期|今日|"
    r"(?:月|火|水|木|金|土|日)曜日|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|"
    r"春|夏|秋|冬)"
    r"(?=\s*(?:頃|ごろ|あたり|前後|くらい|程度|"
    r"から|まで|以降|以前|より後|より前|中|付|現在|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|"
    r"の|時点|では|に|は|、|,|$))"
)
ADDITIONAL_TIME_DETAIL_SURFACE = re.compile(
    rf"(?<![0-9A-Za-z]){RELATIVE_YEAR_COUNT_SURFACE}\s*"
    rf"(?:月|日|週|時|分|秒)(?![0-9A-Za-z])"
    rf"|(?<![0-9A-Za-z]){RELATIVE_YEAR_COUNT_SURFACE}\s*"
    rf"(?:[/.\-]\s*{RELATIVE_YEAR_COUNT_SURFACE}){{1,2}}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])(?:Q[1-4]|[1-4]Q)(?![0-9A-Za-z])"
    r"|(?:前日|翌日|前月|翌月|前週|翌週|前年度|翌年度|"
    r"同日|同月|同時期|元日|正月|年末年始|"
    r"上期|下期|上半期|下半期|四半期|"
    r"月初|月末|年初|年末|上旬|中旬|下旬|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|"
    r"(?:月|火|水|木|金|土|日)曜日)"
)
PURE_TEMPORAL_TARGET_SURFACE = re.compile(
    rf"(?:{RELATIVE_YEAR_COUNT_SURFACE}\s*月"
    rf"(?:\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*日)?"
    r"(?:初|末|上旬|中旬|下旬)?|"
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*日|"
    rf"{RELATIVE_YEAR_COUNT_SURFACE}\s*"
    rf"(?:[/.\-]\s*{RELATIVE_YEAR_COUNT_SURFACE}){{1,2}}|"
    r"(?:Q[1-4]|[1-4]Q)|"
    rf"第?\s*{RELATIVE_YEAR_COUNT_SURFACE}\s*(?:週|四半期)|"
    r"(?:[12]\d{3}|(?:令和|平成|昭和)\s*"
    rf"(?:元|{RELATIVE_YEAR_COUNT_SURFACE}))\s*年(?:度)?"
    r"(?:初|末|上期|下期)?|"
    r"年度(?:初|末|上期|下期)?|"
    r"前日|翌日|前月|翌月|前週|翌週|前年度|翌年度|"
    r"同日|同月|同時期|今日|元日|正月|年末年始|"
    r"上期|下期|上半期|下半期|"
    r"月初|月末|年初|年末|上旬|中旬|下旬|"
    r"午前|午後|正午|朝|昼|夕方|夜|深夜|"
    r"(?:月|火|水|木|金|土|日)曜日|春|夏|秋|冬)"
    r"\s*(?:時点|現在|頃|ごろ|あたり|前後|くらい|程度|中|付)?\Z"
)
ASSIGNMENT_NEGATION_SURFACE = re.compile(
    r"(?:担当|受け持|責任者)"
    r"[^。?？!！\n]{0,40}?"
    r"(?:ない|なかった|なく|ありません|ありませんでした|"
    r"いません|いませんでした|おらず|おりません|"
    r"わけでは|ことがない|ことはない|"
    r"限りません|限らない|不明|未確認|不確か|かどうか|"
    r"以外|除く|除いて|除外)"
)
UNSUPPORTED_ASSIGNMENT_EVENT_SURFACE = re.compile(
    r"(?:担当|受け持|責任者)"
    r"[^。?？!！\n]{0,24}?"
    r"(?:始め|開始|着任|就任|交代|交替|変わ|変更|"
    r"引き継|後任|前任|辞め|退任|離任|終了)"
)
ASSIGNMENT_WHO_SURFACE = re.compile(
    r"(?:誰|どなた)|(?<![0-9A-Za-z])who(?![0-9A-Za-z])",
    re.IGNORECASE,
)
UNSUPPORTED_ASSIGNMENT_OUTPUT_SURFACE = re.compile(
    r"(?:何人|全員|全て|すべて|一覧|複数|"
    r"メール(?:アドレス)?|e-?mail|連絡先|電話(?:番号)?|"
    r"役職|部署|住所|所属|"
    r"いつ|何年|何月|何日|どのくらい|何回)",
    re.IGNORECASE,
)
POSITIVE_ASSIGNMENT_PREDICATE = re.compile(
    r"(?:"
    r"担当(?:を)?していました|担当(?:を)?していた|"
    r"担当(?:を)?しています|担当(?:を)?している|"
    r"担当(?:を)?しました|担当(?:を)?した|担当(?:を)?する|"
    r"担当でした|担当だった|担当|"
    r"受け持っていました|受け持っていた|"
    r"受け持っています|受け持っている|"
    r"受け持ちました|受け持った|受け持つ|受け持"
    r")(?!者|名|候補)"
)
OWNER_ROLE_SURFACE = re.compile(
    r"(?:担当者氏名|担当者名|担当社員|担当者|責任者|主担当|受け持ち?(?=は|が))"
)
ASSIGNMENT_RESPONSIBILITY_SURFACE = re.compile(r"(?:担当|責任者|受け持)")
OWNER_PLACEHOLDER_SURFACE = re.compile(
    r"(?:"
    r"[-ー―—–]+|無|無し|なし(?:未定)?|"
    r"該当なし|該当無し|該当者なし|該当者無し|"
    r"不明|不詳|空欄|"
    r"(?:現在)?(?:担当|担当者)?未(?:定|確定|決定|指定|設定|選任|確認|"
    r"着任|配置|回答|選択|対応|登録|記入|入力|割当|割当て|"
    r"割り当て|アサイン)"
    r"(?:です|予定|状態)?|"
    r"(?:担当|担当者)?(?:調整|確認|選定|検討|選任)"
    r"(?:中|待ち|予定)|要(?:確認|調整|選定)|未選定|"
    r"欠員|空席|募集中|保留|"
    r"(?:担当|担当者|割当|アサイン)(?:なし|無し|不在)|"
    r"notassigned|tobeassigned|noowner|vacant|"
    r"tba|tbc|tbd|pending|na|nil|null(?:値)?|none|unknown(?:yet)?|unassigned"
    r")\Z",
    re.IGNORECASE,
)
OWNER_INSTRUCTION_SURFACE = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?"
    r"|(?:system|developer)\s+(?:prompt|message)"
    r"|(?:reveal|print|show)\s+(?:the\s+)?(?:system\s+prompt|hidden\s+instructions?)"
    r"|指示[^\n]{0,20}(?:無視|破棄)|(?:system|developer)プロンプト",
    re.IGNORECASE,
)
OWNER_UNCERTAIN_SURFACE = re.compile(
    r"[?？]|(?:[（(](?:予定|仮|候補|暫定|provisional)[）)])|"
    r"(?:かも(?:しれない)?|候補|予定|暫定|"
    r"(?<![0-9a-z])provisional|その他)\s*$|"
    r"(?:または|もしくは|あるいは|(?<![0-9a-z])or(?![0-9a-z]))|"
    r"(?:^|[/／])\s*(?:未定|不明|tba|tbc|tbd|unknown|unassigned)"
    r"(?:\s*[/／]|$)|"
    r"(?:未定|不明|tba|tbc|tbd|unknown|unassigned)\s*$",
    re.IGNORECASE,
)
OWNER_NEGATED_SURFACE = re.compile(
    r"(?:ではない|ではありません|ではございません|"
    r"ではなかった|ではなく(?:て)?|でない|でなく|じゃない|じゃありません|"
    r"じゃなく|以外|除く|除いて)"
    r"|(?<![0-9a-z])(?:not|except)(?![0-9a-z])"
    r"|(?<![0-9a-z])no\s+longer(?![0-9a-z])"
    r"|(?<![0-9a-z])neither\b.+\bnor(?![0-9a-z])",
    re.IGNORECASE,
)
OWNER_TRANSITION_SURFACE = re.compile(
    r"(?:->|=>|[→⇒↦])"
    r"|から.+へ"
    r"|(?<![0-9a-z])from\s+.+\s+to(?![0-9a-z])"
    r"|\s+to\s+",
    re.IGNORECASE,
)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
TEMPORAL_PRECISION = "day"
TEMPORAL_BOUNDARY = "inclusive"
TEMPORAL_RESOLUTION_RULE = "calendar_year_offset_clamp"
TEMPORAL_TIMEZONE = "Asia/Tokyo"
DEICTIC_ASSIGNMENT_TARGETS = frozenset((
    "この業務", "その業務", "当該業務", "この仕事", "その仕事", "それ",
))
ASSIGNMENT_TARGET_PATTERNS = (
    re.compile(
        r"(?:the\s+)?owner\s+of\s+(?P<target>[^.。?？!！]{1,80}?)"
        r"(?=\s*(?:[.。?？!！]|$))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<target>[^.。?？!！]{1,80}?)\s*['’]s\s+owner",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<target>[^.。?？!！]{1,80}?)の\s*owner",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:誰|どなた)が(?P<target>[^,、。?？!！]{1,80}?)"
        r"を(?:担当|受け持)"
    ),
    re.compile(
        r"(?P<target>[^,、。?？!！]{1,80}?)の(?:"
        r"担当者氏名|担当者名|担当社員|担当者|責任者|主担当|受け持ち?)"
    ),
    re.compile(
        r"(?P<target>[^。?？!！]{1,80}?)"
        r"は(?:誰|どなた)の(?:担当|受け持)"
    ),
    re.compile(
        r"(?P<target>[^。?？!！]{1,80}?)"
        r"の担当(?:は|が)?(?:誰|どなた)"
    ),
    re.compile(
        r"(?P<target>[^。?？!！]{1,80}?)を(?:誰|どなた)が(?:担当|受け持)"
    ),
    re.compile(
        r"(?P<target>[^.。?？!！]{1,80}?)"
        r"(?:について(?:は)?|では|の場合(?:は)?|において(?:は)?|は)"
        r"[^。?？!！]{0,60}?(?:誰|どなた)が(?:担当|受け持)"
    ),
    re.compile(
        r"(?P<target>[^。?？!！]{1,80}?)を[^。?？!！]{0,60}?"
        r"(?:担当|受け持)[^。?？!！]{0,40}?(?:誰|どなた)"
    ),
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_text(value: object) -> str:
    """Canonicalize one observed scalar before any identity comparison."""
    text = str(value).strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            decoded = text
        if isinstance(decoded, str):
            text = decoded
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    text = "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if unicodedata.category(char) != "Cf"
    )
    return re.sub(r"\s+", " ", text).strip()


def normalize(value: object) -> str:
    return "".join(
        char for char in _canonical_text(value).casefold()
        if char.isalnum() or "ぁ" <= char <= "鿿"
    )


def assignment_target_identity(value: object) -> str:
    """Normalize target presentation without erasing identity punctuation."""
    return _canonical_text(value).casefold()


def _clean_assignment_target_surface(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(
        r"^(?:(?:\s|,|、)+(?:その時|当時|に|では?|の|は))+",
        "",
        normalized,
    ).strip(" \t,、\"’'「」『』")
    normalized = re.sub(
        r"(?:について|では|の場合|において)\s*$", "", normalized,
    )
    normalized = normalized.strip(
        " \t,、\"’'「」『』"
    )
    if not normalized:
        return None
    if assignment_target_identity(normalized) in {
        assignment_target_identity(target) for target in DEICTIC_ASSIGNMENT_TARGETS
    }:
        return None
    return normalized


def extract_temporal_assignment_target(question: str) -> str | None:
    """Extract one exact, explicit assignment target from common JP word orders."""
    normalized = unicodedata.normalize("NFKC", question)
    normalized = EXPLICIT_CURRENT_RELATIVE_ANCHOR.sub(" ", normalized)
    normalized = RELATIVE_YEAR_SURFACE.sub(" ", normalized)
    # Collapse the two possessives left by forms such as
    # "Project Atlasの5年前の担当者".
    normalized = re.sub(
        r"の\s+の(?=\s*(?:担当者|責任者))", "の", normalized,
    )
    normalized = re.sub(
        r"(?:その時|当時|(?:その)?時点(?:で|の)?|(?<!\S)における)",
        " ",
        normalized,
    )
    candidates: dict[str, str] = {}
    for pattern in ASSIGNMENT_TARGET_PATTERNS:
        for match in pattern.finditer(normalized):
            candidate = _clean_assignment_target_surface(match.group("target"))
            if candidate is not None:
                candidates.setdefault(assignment_target_identity(candidate), candidate)
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _mask_explicit_assignment_target(question: str) -> tuple[str, str | None]:
    """Hide exact target text before scanning the remaining temporal grammar."""
    normalized = unicodedata.normalize("NFKC", question)
    target = extract_temporal_assignment_target(normalized)
    if target is None or PURE_TEMPORAL_TARGET_SURFACE.fullmatch(target):
        return normalized, None
    return normalized.replace(target, " " * len(target)), target


def _record_status_scope_text(
    question: str,
    plan_items: Iterable[Mapping[str, Any]],
    temporal_context: Mapping[str, Any] | None = None,
    records: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Exclude an owner target name from final/draft qualifier detection."""
    normalized = unicodedata.normalize("NFKC", question)
    owner_lookup = any(
        item.get("field_name") == "owner" for item in plan_items
    )
    target = (
        temporal_context.get("target")
        if temporal_context is not None
        else extract_temporal_assignment_target(normalized)
        if owner_lookup and plain_assignment_owner_question_supported(normalized)
        else _grounded_record_subject(normalized, records)
    )
    if not isinstance(target, str) or not target:
        return normalized
    return re.sub(
        re.escape(unicodedata.normalize("NFKC", target)),
        lambda match: " " * len(match.group(0)),
        normalized,
        flags=re.IGNORECASE,
    )


def _grounded_record_subject(
    question: str,
    records: Iterable[Mapping[str, Any]],
) -> str | None:
    """Find one structured subject value explicitly present in the question."""
    normalized_question = unicodedata.normalize("NFKC", question)
    candidates: dict[str, str] = {}
    for record in records:
        text = record.get("text")
        if not isinstance(text, str):
            continue
        for label, values in _fields(text).items():
            if not _preferred_subject_label(label) or len(values) != 1:
                continue
            value = _canonical_text(values[0])
            identity = assignment_target_identity(value)
            if (
                len(normalize(value)) < 2
                or FINAL_RECORD_SURFACE.fullmatch(value)
                or NONFINAL_RECORD_SURFACE.fullmatch(value)
            ):
                continue
            if re.search(
                re.escape(unicodedata.normalize("NFKC", value)),
                normalized_question,
                flags=re.IGNORECASE,
            ):
                candidates.setdefault(identity, value)
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _tail_starts_with_target(tail: str, target: str) -> bool:
    candidate = tail.lstrip(" \t\"'「『")
    if not candidate.startswith(target):
        return False
    remainder = candidate[len(target):].lstrip(" \t\"'」』")
    return (
        not remainder
        or remainder.startswith((
            "の", "を", "は", "が", "で", "に", "について",
            "、", ",", "。", ".", "?", "？", "!", "！",
        ))
    )


def _target_has_owned_temporal_segment(target: str) -> bool:
    """Detect a time qualifier embedded as one possessive target segment."""
    segments = [
        segment.strip()
        for segment in re.split(r"\s*の\s*", unicodedata.normalize("NFKC", target))
    ]
    if len(segments) < 2:
        return False
    return any(
        bool(PURE_TEMPORAL_TARGET_SURFACE.fullmatch(segment))
        or segment in {"期首", "期末"}
        for segment in segments
        if segment
    )


def _temporal_owner_question_shape_supported(
    question: str,
    target: str | None,
) -> bool:
    """Accept only full clauses that ask for the owner at one time point."""
    if target is None:
        # Deictic/unresolved targets are rejected later by target validation,
        # without silently promoting them here.
        return True
    skeleton = unicodedata.normalize("NFKC", question).replace(
        target, "TARGET",
    )
    skeleton = RELATIVE_YEAR_SURFACE.sub("TIME", skeleton)
    skeleton = re.sub(
        r"(?:今|現在|現時点)から(?:ちょうど|まさに)?TIME",
        "TIME",
        skeleton,
    )
    skeleton = re.sub(r"(?:ちょうど|まさに)TIME", "TIME", skeleton)
    skeleton = skeleton.translate(str.maketrans(
        "", "", "\"'「」『』()（）[]【】 \t\r\n",
    ))
    skeleton = OWNER_ROLE_SURFACE.sub("ROLE", skeleton)
    skeleton = POSITIVE_ASSIGNMENT_PREDICATE.sub("ASSIGN", skeleton)
    skeleton = skeleton.rstrip("。.?？!！")
    skeleton = re.sub(
        r"(?:です|でした|でしょう)?か$", "", skeleton,
    )
    skeleton = re.sub(r"(?:です|でした)$", "", skeleton)
    role = r"(?:ROLE|ASSIGN)"
    who = r"(?:誰|どなた)"
    shapes = (
        rf"TIME(?:(?:の)?時点(?:で|では)|には|では|は|に|で|、|,)?"
        rf"{who}がTARGETをASSIGN",
        rf"TIME(?:(?:の)?時点(?:で|では)|には|では|は|に|で|、|,)?"
        rf"TARGETを{who}がASSIGN",
        rf"TIME(?:に|、|,)TARGETをASSIGNのは{who}",
        rf"TARGETをTIMEにASSIGNのは{who}",
        rf"TIME(?:の時点(?:で|では?)|における)TARGETの{role}は{who}",
        rf"TIME時点のTARGETの{role}は{who}",
        rf"TIMEのTARGETの{role}は{who}",
        rf"TIME[、,]TARGETの{role}は{who}",
        rf"TIMEのTARGETは{who}(?:がASSIGN|のASSIGN)",
        rf"TARGETのTIMEの{role}は{who}",
        rf"TARGET(?:は|について[、,]?|では|の場合[、,]?|"
        rf"において(?:は)?)TIME(?:は|[、,])?{who}がASSIGN",
    )
    return any(re.fullmatch(shape, skeleton) for shape in shapes)


def plain_assignment_owner_question_supported(question: str) -> bool:
    """Allow only positive, single-field owner questions without a time scope."""
    target = extract_temporal_assignment_target(question)
    if target is None:
        return False
    skeleton = unicodedata.normalize("NFKC", question).replace(
        target, "TARGET",
    )
    skeleton = skeleton.translate(str.maketrans(
        "", "", "\"'「」『』()（）[]【】 \t\r\n",
    ))
    skeleton = OWNER_ROLE_SURFACE.sub("ROLE", skeleton)
    skeleton = POSITIVE_ASSIGNMENT_PREDICATE.sub("ASSIGN", skeleton)
    skeleton = skeleton.rstrip("。.?？!！")
    skeleton = re.sub(r"(?:です|でした|でしょう)?か$", "", skeleton)
    skeleton = re.sub(r"(?:です|でした)$", "", skeleton)
    role = r"(?:ROLE|ASSIGN)"
    who = r"(?:誰|どなた)"
    shapes = (
        rf"{who}がTARGETをASSIGN",
        rf"TARGETを{who}がASSIGN",
        rf"TARGETをASSIGNのは{who}",
        rf"TARGETの{role}は{who}",
        rf"TARGETの{role}が{who}",
        rf"TARGETは{who}(?:がASSIGN|のASSIGN)",
        rf"TARGET(?:について[、,]?|では|の場合[、,]?|"
        rf"において(?:は)?)?{who}がASSIGN",
    )
    english_shapes = (
        r"whois(?:the)?owneroftarget",
        r"targetsowner(?:iswho)?",
        r"targetのowner(?:は|が)(?:誰|どなた)",
    )
    return (
        any(re.fullmatch(shape, skeleton) for shape in shapes)
        or any(
            re.fullmatch(shape, skeleton.casefold())
            for shape in english_shapes
        )
    )


def _relative_year_structure_unsupported(question: str, target: str | None) -> bool:
    """Allow only run-date anchored N-years-ago syntax implemented by this contract."""
    normalized = unicodedata.normalize("NFKC", question)
    masked = (
        normalized.replace(target, " " * len(target))
        if target is not None else normalized
    )
    for match in RELATIVE_YEAR_SURFACE.finditer(masked):
        prefix = masked[:match.start()].rstrip()
        original_prefix = normalized[:match.start()].rstrip()
        target_before_time = bool(
            target is not None
            and re.search(rf"{re.escape(target)}\s*の\s*$", original_prefix)
        )
        compact_prefix = re.sub(r"\s+", "", prefix)
        compact_prefix = compact_prefix.translate(str.maketrans(
            "", "", "\"'「」『』()（）[]【】",
        ))
        if compact_prefix.endswith(("ちょうど", "まさに")):
            compact_prefix = compact_prefix.removesuffix(
                "ちょうど" if compact_prefix.endswith("ちょうど") else "まさに"
            )
        current_anchor = re.search(
            r"(?:今|現在|現時点)から"
            r"(?:(?:数えて|遡って|さかのぼって)|"
            r"(?:を)?(?:基準|起点)(?:に|として)?)?$",
            compact_prefix,
        )
        if current_anchor is not None:
            compact_prefix = compact_prefix[:current_anchor.start()]
        if target_before_time:
            compact_prefix = ""
        if compact_prefix not in {
            "", "、", ",", "は", "を", "で", "では",
            "について", "について、", "について,",
            "の場合", "の場合、", "の場合,",
            "において", "においては",
            "の担当者は", "の責任者は",
            "の担当者について", "の責任者について",
        }:
            return True

        # Target masking preserves string length, so these offsets still point
        # to the original question surface.
        following = normalized[match.end():]
        possessive = re.match(r"\s*の\s*", following)
        if possessive is None:
            continue
        tail = following[possessive.end():].lstrip()
        if re.match(r"時点(?=\s*(?:で|の|は|、|,|$))", tail):
            continue
        if target is not None and _tail_starts_with_target(tail, target):
            continue
        if target_before_time and re.match(
            r"(?:担当者|担当者名|責任者|主担当|担当)"
            r"(?=\s*(?:は|が|を|、|,|$))",
            tail,
        ):
            continue
        return True
    return False


def temporal_assignment_context_unsupported(question: str) -> bool:
    """Reject ranges, approximations, and mixed absolute/relative anchors."""
    normalized, target = _mask_explicit_assignment_target(question)
    target_has_time_modifier = bool(
        target is not None and _target_has_owned_temporal_segment(target)
    )
    if target is not None:
        target_time_matches = sorted(
            (
                *NONCANONICAL_ASSIGNMENT_TIME_SURFACE.finditer(target),
                *TARGET_ABSOLUTE_TIME_MODIFIER_SURFACE.finditer(target),
                *ADDITIONAL_TIME_DETAIL_SURFACE.finditer(target),
                *TARGET_EVENT_TIME_MODIFIER_SURFACE.finditer(target),
            ),
            key=lambda match: (match.start(), -match.end()),
        )
        for target_time_match in target_time_matches:
            before = target[:target_time_match.start()].rstrip()
            after = target[target_time_match.end():].lstrip()
            if (not before or before.endswith("の")) and (
                not after or after.startswith("の")
            ):
                target_has_time_modifier = True
                break
    exact_relative = RELATIVE_YEAR_SURFACE.search(normalized) is not None
    temporal_remainder = RELATIVE_YEAR_SURFACE.sub(" ", normalized)
    return bool(
        target_has_time_modifier
        or (
            exact_relative
            and (
                not ASSIGNMENT_WHO_SURFACE.search(normalized)
                or UNSUPPORTED_ASSIGNMENT_OUTPUT_SURFACE.search(normalized)
                or ASSIGNMENT_NEGATION_SURFACE.search(normalized)
                or UNSUPPORTED_ASSIGNMENT_EVENT_SURFACE.search(normalized)
                or not _temporal_owner_question_shape_supported(
                    question, target,
                )
            )
        )
        or _relative_year_structure_unsupported(question, target)
        or (
            not exact_relative
            and RELATIVE_YEAR_BROAD_SURFACE.search(normalized)
        )
        or RELATIVE_YEAR_LIKE_SURFACE.search(normalized)
        or RELATIVE_YEAR_BOUND_SURFACE.search(normalized)
        or OTHER_ASSIGNMENT_TIME_SURFACE.search(normalized)
        or NON_YEAR_RELATIVE_TIME_SURFACE.search(normalized)
        or ABSOLUTE_DATE_SURFACE.search(normalized)
        or RELATIVE_YEAR_APPROX_OR_RANGE.search(normalized)
        or ABSOLUTE_YEAR_SURFACE.search(normalized)
        or ADDITIONAL_TIME_DETAIL_SURFACE.search(temporal_remainder)
        or UNPARSED_YEARS_AGO_SURFACE.search(temporal_remainder)
        or (
            not exact_relative
            and NONCANONICAL_ASSIGNMENT_TIME_SURFACE.search(normalized)
        )
        or (
            not exact_relative
            and GENERIC_ASSIGNMENT_TIME_SURFACE.search(normalized)
        )
        or (
            exact_relative
            and CURRENT_TIME_SURFACE.search(normalized)
            and EXPLICIT_CURRENT_RELATIVE_ANCHOR.search(normalized) is None
        )
        or (
            exact_relative
            and RELATIVE_YEAR_CALENDAR_DETAIL.search(normalized)
        )
    )


def _assignment_plan_intent(
    question: str,
    question_plan: Mapping[str, Any],
) -> bool:
    """Identify an assignee lookup before accepting any temporal grammar."""
    if question_plan.get("relation") == "responsible_for":
        return True
    raw_items = question_plan.get("items")
    owner_plan = bool(
        isinstance(raw_items, list)
        and any(
            isinstance(item, Mapping)
            and _plan_item_record_field(item) == "owner"
            for item in raw_items
        )
    )
    if not owner_plan:
        return False
    normalized_question = unicodedata.normalize("NFKC", question)
    return bool(
        ASSIGNMENT_RESPONSIBILITY_SURFACE.search(normalized_question)
        or (
            ASSIGNMENT_WHO_SURFACE.search(normalized_question)
            and any(
                _record_alias_mentioned(normalized_question, alias)
                for alias in RECORD_LOOKUP_FIELD_ALIASES["owner"]
            )
        )
    )


def _strict_iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _japanese_integer(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if normalized.isdecimal():
        parsed = int(normalized)
        return parsed if 1 <= parsed <= 99 else None
    digits = {
        "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    if "十" in normalized:
        if normalized.count("十") != 1:
            return None
        tens_text, ones_text = normalized.split("十")
        if len(tens_text) > 1 or len(ones_text) > 1:
            return None
        tens = digits.get(tens_text, 1) if tens_text else 1
        ones = digits.get(ones_text, 0) if ones_text else 0
        if tens is None or ones is None or tens == 0:
            return None
        parsed = tens * 10 + ones
        return parsed if 1 <= parsed <= 99 else None
    if not normalized or any(char not in digits for char in normalized):
        return None
    parsed = int("".join(str(digits[char]) for char in normalized))
    return parsed if 1 <= parsed <= 99 else None


def _subtract_calendar_years_clamped(reference: date, years: int) -> date | None:
    target_year = reference.year - years
    if target_year < 1:
        return None
    try:
        return reference.replace(year=target_year)
    except ValueError:
        # The only valid month/day that needs clamping is February 29.
        return reference.replace(year=target_year, day=28)


def _temporal_question_matches(question: str) -> list[dict[str, Any]]:
    normalized = unicodedata.normalize("NFKC", question)
    matches = []
    for match in RELATIVE_YEAR_SURFACE.finditer(normalized):
        years = _japanese_integer(match.group("count"))
        matches.append({
            "expression": re.sub(r"\s+", "", match.group("expression")),
            "years": years,
        })
    return matches


def _validated_temporal_context(
    question: str,
    question_plan: Mapping[str, Any],
    reference_date: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate a planner-supplied temporal scope against deterministic inputs."""
    question_matches = _temporal_question_matches(question)
    assignment_intent = _assignment_plan_intent(question, question_plan)
    if assignment_intent and temporal_assignment_context_unsupported(question):
        return None, {
            "code": "temporal_expression_context_unsupported",
            "detail": (
                "Only one exact N-years-ago time point is supported; "
                "other time expressions, ranges, approximations, and mixed "
                "year anchors are not."
            ),
        }
    has_scope = "temporal_scope" in question_plan
    if (
        assignment_intent
        and not question_matches
        and not has_scope
        and not plain_assignment_owner_question_supported(question)
    ):
        return None, {
            "code": "assignment_question_shape_unsupported",
            "detail": (
                "Only a positive owner question without a time expression, "
                "or one exact N-years-ago owner question, is supported."
            ),
        }
    if not has_scope:
        if question_matches:
            return None, {
                "code": "temporal_scope_missing",
                "detail": "A relative-year question requires a typed temporal_scope.",
            }
        return None, None

    raw_scope = question_plan.get("temporal_scope")
    if not isinstance(raw_scope, Mapping):
        return None, {
            "code": "temporal_scope_shape_invalid",
            "detail": "temporal_scope must be an object.",
        }
    required_keys = (
        "expression", "reference_date", "as_of", "precision", "boundary",
        "resolution_rule", "timezone",
    )
    missing = [key for key in required_keys if key not in raw_scope]
    non_strings = [
        key for key in required_keys
        if key in raw_scope and not isinstance(raw_scope.get(key), str)
    ]
    if missing or non_strings:
        return None, {
            "code": "temporal_scope_shape_invalid",
            "detail": {"missing": missing, "non_string": non_strings},
        }
    scope = {key: str(raw_scope[key]).strip() for key in required_keys}
    if any(not value for value in scope.values()):
        return None, {
            "code": "temporal_scope_shape_invalid",
            "detail": "temporal_scope values must be non-empty strings.",
        }
    if (
        scope["precision"] != TEMPORAL_PRECISION
        or scope["boundary"] != TEMPORAL_BOUNDARY
        or scope["resolution_rule"] != TEMPORAL_RESOLUTION_RULE
        or scope["timezone"] != TEMPORAL_TIMEZONE
    ):
        return None, {
            "code": "temporal_scope_contract_invalid",
            "detail": {
                "precision": scope["precision"],
                "boundary": scope["boundary"],
                "resolution_rule": scope["resolution_rule"],
                "timezone": scope["timezone"],
            },
        }

    expected_reference = _strict_iso_date(reference_date)
    planned_reference = _strict_iso_date(scope["reference_date"])
    planned_as_of = _strict_iso_date(scope["as_of"])
    if expected_reference is None or planned_reference is None or planned_as_of is None:
        return None, {
            "code": "temporal_date_invalid",
            "detail": {
                "expected_reference_date": reference_date,
                "reference_date": scope["reference_date"],
                "as_of": scope["as_of"],
            },
        }
    if planned_reference != expected_reference:
        return None, {
            "code": "temporal_reference_date_mismatch",
            "detail": {
                "expected": expected_reference.isoformat(),
                "observed": planned_reference.isoformat(),
            },
        }
    if len(question_matches) != 1:
        return None, {
            "code": "temporal_expression_ambiguous",
            "detail": question_matches,
        }
    expression = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", scope["expression"])
    )
    question_expression = question_matches[0]
    if question_expression["years"] is None:
        return None, {
            "code": "temporal_expression_out_of_range",
            "detail": question_expression["expression"],
        }
    if expression != question_expression["expression"]:
        return None, {
            "code": "temporal_expression_not_grounded",
            "detail": {
                "planned": expression,
                "question": question_expression["expression"],
            },
        }
    expected_as_of = _subtract_calendar_years_clamped(
        expected_reference, question_expression["years"]
    )
    if expected_as_of is None:
        return None, {
            "code": "temporal_as_of_out_of_range",
            "detail": {
                "reference_date": expected_reference.isoformat(),
                "years": question_expression["years"],
            },
        }
    if planned_as_of != expected_as_of or planned_as_of > planned_reference:
        return None, {
            "code": "temporal_as_of_mismatch",
            "detail": {
                "expected": expected_as_of.isoformat(),
                "observed": planned_as_of.isoformat(),
            },
        }

    target = question_plan.get("target")
    relation = question_plan.get("relation")
    grounded_target = extract_temporal_assignment_target(question)
    if (
        not isinstance(target, str)
        or not target.strip()
        or grounded_target is None
        or assignment_target_identity(target)
        != assignment_target_identity(grounded_target)
    ):
        return None, {
            "code": "temporal_target_invalid",
            "detail": str(target or ""),
        }
    if relation != "responsible_for":
        return None, {
            "code": "temporal_relation_invalid",
            "detail": str(relation or ""),
        }
    canonical_scope = {
        **scope,
        "expression": expression,
    }
    return {
        "scope": canonical_scope,
        "target": grounded_target,
        "relation": relation,
        "years": question_expression["years"],
    }, None


def _structured_date(value: str) -> date | None:
    normalized = unicodedata.normalize("NFKC", value).strip()
    # openpyxl serializes a date-formatted Excel cell as an ISO datetime when
    # the underlying cell value is a datetime. Day-precision assignment
    # periods may safely accept that native midnight representation.
    midnight = re.fullmatch(
        r"(?P<date>\d{4}-\d{2}-\d{2})T00:00:00(?:\.0+)?",
        normalized,
    )
    if midnight:
        normalized = midnight.group("date")
    match = re.fullmatch(
        r"(?P<year>\d{4})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})"
        r"\s*(?:月|[-/.])\s*(?P<day>\d{1,2})\s*日?",
        normalized,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _column_number(column: str) -> int:
    result = 0
    for char in column.upper():
        result = result * 26 + ord(char) - 64
    return result


def _fields(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        match = FIELD_LINE.match(unicodedata.normalize("NFKC", raw_line).strip())
        if not match:
            continue
        label = match.group("label").strip()
        result.setdefault(label, []).append(match.group("value").strip())
    return result


def _evidence_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    evidence_id = str(record.get("evidence_id", "")).strip()
    document_id = str(record.get("document_id", "")).strip()
    relative_path = str(record.get("relative_path", "")).strip()
    locator = record.get("locator")
    text = record.get("text", record.get("observed_text", ""))
    if not evidence_id or not document_id or not relative_path or not isinstance(locator, Mapping):
        return None
    if not isinstance(text, str) or not text:
        return None
    actual_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    claimed_sha256 = str(record.get("observed_sha256", "")).strip()
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "relative_path": relative_path,
        "locator": dict(locator),
        "text": text,
        "observed_sha256": actual_sha256,
        "integrity_error": (
            "observed_sha256_mismatch"
            if claimed_sha256 and claimed_sha256 != actual_sha256 else ""
        ),
    }


def _question_scope(question: str) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", question)
    match = YEAR_MONTH.search(normalized)
    return {
        "year": int(match.group("year")) if match else None,
        "month": int(match.group("month")) if match else None,
    }


def _scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    year = scope.get("year")
    month = scope.get("month")
    if year is None or month is None:
        return True
    normalized = unicodedata.normalize("NFKC", path)
    patterns = (
        f"{year}.{month:02d}", f"{year}-{month:02d}", f"{year}_{month:02d}",
        f"{year}/{month:02d}", f"{year}{month:02d}", f"{year}年{month}月",
    )
    return any(pattern in normalized for pattern in patterns)


def _question_subject(question: str) -> str:
    value = unicodedata.normalize("NFKC", question)
    for term in GENERIC_QUERY_TERMS:
        value = value.replace(term, " ")
    value = re.sub(r"20\d{2}\s*年\s*(?:1[0-2]|0?[1-9])\s*月", " ", value)
    value = re.sub(r"[\s、。,.:;:!?！？・/／()（）のはをがでにとして]+", " ", value)
    return normalize(value)


def _subject_score(question: str, label: str) -> float:
    q = normalize(question)
    subject = _question_subject(question)
    candidate = normalize(label)
    if not candidate:
        return 0.0
    if candidate in q:
        return 1.0
    if candidate in subject:
        return 0.95
    if not subject:
        return 0.0
    qgrams = {subject[index:index + 2] for index in range(max(0, len(subject) - 1))}
    cgrams = {candidate[index:index + 2] for index in range(max(0, len(candidate) - 1))}
    return len(qgrams & cgrams) / max(1, len(cgrams))


def _count_intent(question: str) -> tuple[bool, list[str]]:
    surfaces = [surface for surface in COUNT_SURFACES if surface in question]
    return bool(surfaces), surfaces


def _record_field_name(value: object) -> str | None:
    normalized = normalize(value)
    if not normalized:
        return None
    for field_name, aliases in RECORD_LOOKUP_FIELD_ALIASES.items():
        if normalized in {normalize(alias) for alias in aliases}:
            return field_name
    return None


def _record_alias_mentioned(value: object, alias: str) -> bool:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized_alias = unicodedata.normalize("NFKC", alias).casefold()
    if normalized_alias.isascii():
        tokens = re.findall(r"[0-9a-z]+", normalized_alias)
        if not tokens:
            return False
        phrase = r"[^0-9a-z]+".join(map(re.escape, tokens))
        return re.search(
            rf"(?<![0-9a-z]){phrase}(?![0-9a-z])",
            text,
        ) is not None
    return normalized_alias in text


def _mentioned_record_fields(value: object) -> list[str]:
    if not str(value or "").strip():
        return []
    return [
        field_name
        for field_name, aliases in RECORD_LOOKUP_FIELD_ALIASES.items()
        if any(_record_alias_mentioned(value, alias) for alias in aliases)
    ]


def _plan_item_record_field(raw: Mapping[str, Any]) -> str | None:
    for key in ("field_name", "field", "label"):
        if key in raw:
            field_name = _record_field_name(raw.get(key))
            if field_name is not None:
                return field_name
    mentioned = _mentioned_record_fields(raw.get("label"))
    if len(mentioned) == 1:
        return mentioned[0]
    return None


def _record_label_residue_grounded(
    label: str,
    field_name: str,
    question: str,
) -> bool:
    """Allow a planner paraphrase only when its non-field residue is in the query."""
    label_key = normalize(label)
    question_key = normalize(question)
    for alias in sorted(
        RECORD_LOOKUP_FIELD_ALIASES[field_name],
        key=lambda value: len(normalize(value)),
        reverse=True,
    ):
        alias_key = normalize(alias)
        if alias_key and alias_key in label_key:
            residue = label_key.replace(alias_key, "")
            return not residue or residue in question_key
    return False


def _record_lookup_plan(
    question_plan: Mapping[str, Any] | None,
    *,
    activate_unknown: bool = False,
    question: str = "",
) -> list[dict[str, Any]] | None:
    """Preserve any plan tied to a known record field for fail-closed lookup.

    A valid single alias is canonicalized. Unknown, multi-field, and malformed
    items remain explicit markers so the caller can hold them fail-closed. A
    plan containing only unknown generic items stays outside record lookup
    unless the raw question itself explicitly names a known record field.
    """
    if not isinstance(question_plan, Mapping):
        return None
    raw_items = question_plan.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return ([{
            "item_id": "",
            "label": "",
            "field_name": None,
            "mentioned_field_names": [],
            "plan_errors": ["items_invalid"],
        }] if activate_unknown else None)
    items: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()
    known_field_count = 0
    for index, raw in enumerate(raw_items, 1):
        if not isinstance(raw, Mapping):
            items.append({
                "item_id": f"item_{index}",
                "label": "",
                "field_name": None,
                "mentioned_field_names": [],
                "plan_errors": ["item_not_object"],
            })
            continue
        item_id = str(raw.get("item_id", "")).strip()
        label = str(raw.get("label", "")).strip()
        plan_errors = []
        if not item_id:
            plan_errors.append("item_id_missing")
            item_id = f"item_{index}"
        elif item_id in seen_item_ids:
            plan_errors.append("item_id_duplicate")
        if not label:
            plan_errors.append("label_missing")
        field_name = _plan_item_record_field(raw)
        mentioned_field_names = _mentioned_record_fields(label)
        exact_label_field = _record_field_name(label)
        if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in label):
            plan_errors.append("label_control_character")
        if (
            field_name is not None
            and exact_label_field != field_name
            and not _record_label_residue_grounded(
                label, field_name, question,
            )
        ):
            plan_errors.append("label_not_exact_field_alias")
        if field_name is not None or mentioned_field_names:
            known_field_count += 1
        seen_item_ids.add(item_id)
        items.append({
            "item_id": item_id,
            "label": label,
            "field_name": field_name,
            "mentioned_field_names": mentioned_field_names,
            "plan_errors": plan_errors,
        })
    return items if known_field_count or activate_unknown else None


def _record_lookup_plan_hash(question_plan: Mapping[str, Any]) -> str:
    return stable_hash(dict(question_plan))


def _ungrounded_record_plan_items(
    question: str,
    plan_items: list[dict[str, Any]],
) -> list[str]:
    return [
        item["item_id"]
        for item in plan_items
        if item["field_name"] is not None
        if not any(
            _record_alias_mentioned(question, alias)
            for alias in RECORD_LOOKUP_FIELD_ALIASES[item["field_name"]]
        )
    ]


def _status_label(label: str) -> bool:
    return normalize(label) in {normalize(alias) for alias in RECORD_STATUS_LABEL_ALIASES}


def _preferred_subject_label(label: str) -> bool:
    return normalize(label) in {normalize(alias) for alias in RECORD_SUBJECT_LABEL_ALIASES}


def _temporal_subject_label(label: str) -> bool:
    return normalize(label) in {
        normalize(alias) for alias in TEMPORAL_RECORD_SUBJECT_LABEL_ALIASES
    }


def _owner_subject_label(label: str) -> bool:
    """Broad subject suspicion used only for owner-row completeness checks."""
    return _preferred_subject_label(label) or _temporal_subject_label(label)


def _single_field_entries(
    fields: Mapping[str, list[str]],
    field_name: str,
) -> list[dict[str, str]]:
    entries = []
    for label, values in fields.items():
        if _record_field_name(label) != field_name:
            continue
        if len(values) != 1 or not values[0].strip():
            entries.append({"label": label, "value": "", "cardinality": str(len(values))})
        else:
            entries.append({"label": label, "value": values[0].strip(), "cardinality": "1"})
    return sorted(entries, key=lambda item: (normalize(item["label"]), item["value"]))


def _single_temporal_entry(
    fields: Mapping[str, list[str]],
    aliases: tuple[str, ...],
) -> tuple[dict[str, str] | None, str | None]:
    accepted = {normalize(alias) for alias in aliases}
    matches = []
    for label, values in fields.items():
        if normalize(label) not in accepted:
            continue
        if len(values) != 1 or not values[0].strip():
            return None, "empty_or_ambiguous"
        matches.append({"label": label, "value": values[0].strip()})
    if not matches:
        return None, "missing"
    if len(matches) != 1:
        return None, "ambiguous_aliases"
    return matches[0], None


def _has_possible_assignment_period(
    fields: Mapping[str, list[str]],
) -> bool:
    normalized_labels = {normalize(label) for label in fields}
    starts = {
        normalize(alias) for alias in POSSIBLE_PERIOD_START_LABEL_ALIASES
    }
    ends = {
        normalize(alias) for alias in POSSIBLE_PERIOD_END_LABEL_ALIASES
    }
    return bool(normalized_labels & starts) and bool(normalized_labels & ends)


def _has_assignment_time_metadata(
    fields: Mapping[str, list[str]],
) -> bool:
    """Detect time-versioned ownership without guessing from generic one-sided dates."""
    normalized_labels = {normalize(label) for label in fields}
    strict = {
        normalize(alias)
        for alias in (*TEMPORAL_START_LABEL_ALIASES, *TEMPORAL_END_LABEL_ALIASES)
    }
    combined = {
        normalize(alias) for alias in POSSIBLE_COMBINED_PERIOD_LABEL_ALIASES
    }
    return bool(normalized_labels & (strict | combined)) or (
        _has_possible_assignment_period(fields)
    )


def _coordinate_less_target_row_like(text: str, target: str) -> bool:
    """Recognize unlabeled arrays/delimited rows that need a dedicated adapter."""
    canonical_target = assignment_target_identity(target)
    stripped = text.strip()
    if not stripped:
        return False
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    if isinstance(decoded, list):
        scalars = [
            item for item in decoded
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return len(scalars) >= 2 and any(
            assignment_target_identity(item) == canonical_target
            for item in scalars
        )
    parts = [
        part.strip(" \t\r\n\"'「」『』[]")
        for part in re.split(r"[\n\t,、，;；|｜/／•・]+", stripped)
        if part.strip(" \t\r\n\"'「」『』[]")
    ]
    return len(parts) >= 2 and any(
        assignment_target_identity(part) == canonical_target for part in parts
    )


def _orphan_owner_row_units(
    records: list[dict[str, Any]],
    target: str,
) -> list[dict[str, Any]]:
    """Find target+owner spreadsheet rows that lack their row SearchUnit."""
    row_units: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    for record in records:
        locator = record["locator"]
        sheet_name = locator.get("sheet_name")
        row_index = locator.get("row_index")
        if (
            isinstance(sheet_name, str)
            and isinstance(row_index, int)
            and "cell" not in locator
        ):
            row_units.setdefault((
                record["document_id"], record["relative_path"],
                sheet_name, row_index,
            ), []).append(record)
    cells = []
    for record in records:
        locator = record["locator"]
        sheet_name = locator.get("sheet_name")
        cell_match = CELL_COORDINATE.fullmatch(
            str(locator.get("cell", "")).strip()
        )
        if not isinstance(sheet_name, str) or cell_match is None:
            continue
        cells.append({
            "record": record,
            "table": (
                record["document_id"], record["relative_path"], sheet_name,
            ),
            "column": cell_match.group("column").upper(),
            "row": int(cell_match.group("row")),
        })

    header_roles: dict[tuple[tuple[str, str, str], str, int], set[str]] = {}
    for info in cells:
        label = _decode_json_string_literal(info["record"]["text"])
        roles = set()
        if _owner_subject_label(label):
            roles.add("subject")
        if _record_field_name(label) == "owner":
            roles.add("owner")
        if normalize(label) in {
            normalize(alias) for alias in POSSIBLE_PERIOD_START_LABEL_ALIASES
        }:
            roles.add("period_start")
        if normalize(label) in {
            normalize(alias) for alias in POSSIBLE_PERIOD_END_LABEL_ALIASES
        }:
            roles.add("period_end")
        if roles:
            header_roles[(info["table"], info["column"], info["row"])] = roles

    def nearest_roles(
        table: tuple[str, str, str], column: str, row: int,
    ) -> set[str]:
        candidates = [
            (header_row, roles)
            for (header_table, header_column, header_row), roles
            in header_roles.items()
            if header_table == table and header_column == column and header_row < row
        ]
        if not candidates:
            return set()
        nearest = max(header_row for header_row, _roles in candidates)
        return set().union(*(
            roles for header_row, roles in candidates if header_row == nearest
        ))

    by_row: dict[tuple[tuple[str, str, str], int], list[dict[str, Any]]] = {}
    for info in cells:
        by_row.setdefault((info["table"], info["row"]), []).append(info)

    issues = []
    for (table, row), row_cells in by_row.items():
        target_cells = [
            info for info in row_cells
            if "subject" in nearest_roles(table, info["column"], row)
            and _raw_value_matches(info["record"]["text"], target)
        ]
        owner_cells = [
            info for info in row_cells
            if "owner" in nearest_roles(table, info["column"], row)
        ]
        period_start_cells = [
            info for info in row_cells
            if "period_start" in nearest_roles(table, info["column"], row)
        ]
        period_end_cells = [
            info for info in row_cells
            if "period_end" in nearest_roles(table, info["column"], row)
        ]
        if target_cells:
            projected_rows = row_units.get((*table, row), [])
            valid_projected_rows = []
            for projected in projected_rows:
                fields = _fields(projected["text"])
                subject_values = [
                    value
                    for label, values in fields.items()
                    if _owner_subject_label(label)
                    for value in values
                    if _raw_value_matches(value, target)
                ]
                if subject_values:
                    valid_projected_rows.append(projected)
            if len(valid_projected_rows) == 1:
                continue
            issues.append({
                "document_id": table[0],
                "relative_path": table[1],
                "sheet_name": table[2],
                "row_index": row,
                "target_evidence_ids": sorted(
                    info["record"]["evidence_id"] for info in target_cells
                ),
                "owner_evidence_ids": sorted(
                    info["record"]["evidence_id"] for info in owner_cells
                ),
                "period_evidence_ids": sorted(
                    info["record"]["evidence_id"]
                    for info in (*period_start_cells, *period_end_cells)
                ),
                "row_evidence_ids": sorted(
                    record["evidence_id"] for record in projected_rows
                ),
                "reason": "row_search_unit_missing_or_invalid",
            })
    return sorted(
        issues,
        key=lambda item: (
            item["document_id"], item["relative_path"],
            item["sheet_name"], item["row_index"],
        ),
    )


def _record_lookup_candidates(
    records: list[dict[str, Any]],
    question: str,
    plan_items: list[dict[str, Any]],
    require_final: bool,
    temporal_context: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    question_normalized = normalize(question)
    candidates: list[dict[str, Any]] = []
    subject_rows = 0
    missing_fields: set[str] = set()
    ambiguous_fields: set[str] = set()
    nonfinal_statuses: set[str] = set()
    temporal_missing: list[dict[str, Any]] = []
    temporal_invalid: list[dict[str, Any]] = []
    temporal_excluded: list[dict[str, Any]] = []
    temporal_excluded_candidates: list[dict[str, Any]] = []
    temporal_subject_issues: list[dict[str, Any]] = []
    temporal_locator_issues: list[dict[str, Any]] = []
    temporal_owner_invalid: list[dict[str, Any]] = []
    owner_temporal_scope_required: list[dict[str, Any]] = []
    owner_row_unit_issues: list[dict[str, Any]] = []
    temporal_status_invalid: list[dict[str, Any]] = []
    requested_names = {item["field_name"] for item in plan_items}
    owner_lookup = "owner" in requested_names
    as_of = (
        _strict_iso_date(temporal_context["scope"]["as_of"])
        if temporal_context is not None else None
    )
    owner_target = (
        temporal_context["target"]
        if temporal_context is not None
        else extract_temporal_assignment_target(question)
        if owner_lookup else None
    )
    if owner_lookup and owner_target is not None:
        target_identity = assignment_target_identity(owner_target)
        owner_row_unit_issues = _orphan_owner_row_units(records, owner_target)
        subject_label = (
            _owner_subject_label
            if temporal_context is not None else _preferred_subject_label
        )
        relevant_tables: set[tuple[str, str, str]] = set()
        row_infos = []
        temporal_labels = {
            normalize(alias)
            for alias in (
                *TEMPORAL_START_LABEL_ALIASES,
                *TEMPORAL_END_LABEL_ALIASES,
            )
        }
        for record in records:
            locator = record["locator"]
            if (
                isinstance(locator.get("row_index"), int)
                and isinstance(locator.get("sheet_name"), str)
            ):
                continue
            if CELL_COORDINATE.fullmatch(str(locator.get("cell", "")).strip()):
                continue
            fields = _fields(record["text"])
            has_temporal_field = any(
                normalize(label) in temporal_labels for label in fields
            )
            has_owner_field = any(
                _record_field_name(label) == "owner" for label in fields
            )
            has_possible_period = _has_possible_assignment_period(fields)
            possible_subject_values = [
                value
                for label, values in fields.items()
                if _record_field_name(label) != "owner"
                if not _status_label(label)
                if normalize(label) not in temporal_labels
                for value in values
            ]
            exact_target_in_possible = any(
                assignment_target_identity(value) == target_identity
                for value in possible_subject_values
            )
            untyped_date_count = sum(
                _structured_date(value) is not None
                for values in fields.values()
                for value in values
            )
            assignment_shaped = has_owner_field or (
                temporal_context is not None
                and (
                    has_temporal_field
                    or has_possible_period
                    or _has_assignment_time_metadata(fields)
                    or (
                        exact_target_in_possible
                        and (untyped_date_count >= 2 or len(fields) >= 4)
                    )
                )
            )
            target_mentioned = normalize(owner_target) in normalize(record["text"])
            has_owner_surface = any(
                _record_alias_mentioned(record["text"], alias)
                for alias in RECORD_LOOKUP_FIELD_ALIASES["owner"]
            )
            has_date_surface = bool(re.search(
                r"[12]\d{3}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}",
                unicodedata.normalize("NFKC", record["text"]),
            ))
            if target_mentioned and (
                assignment_shaped
                or has_owner_surface
                or has_date_surface
                or _coordinate_less_target_row_like(record["text"], owner_target)
            ):
                temporal_locator_issues.append({
                    "evidence_id": record["evidence_id"],
                    "locator": dict(locator),
                    "reason": "assignment_row_locator_unsupported",
                })
                continue
            if assignment_shaped and any(
                assignment_target_identity(value) == target_identity
                for value in possible_subject_values
            ):
                temporal_locator_issues.append({
                    "evidence_id": record["evidence_id"],
                    "locator": dict(locator),
                    "reason": "assignment_row_locator_unsupported",
                })
        for record in records:
            locator = record["locator"]
            row_index = locator.get("row_index")
            sheet_name = locator.get("sheet_name")
            if not isinstance(row_index, int) or not isinstance(sheet_name, str):
                continue
            fields = _fields(record["text"])
            table_key = (
                record["document_id"], record["relative_path"], sheet_name,
            )
            row_infos.append((record, row_index, sheet_name, table_key, fields))
            has_temporal_field = any(
                normalize(label) in temporal_labels for label in fields
            )
            has_owner_field = any(
                _record_field_name(label) == "owner" for label in fields
            )
            has_possible_period = _has_possible_assignment_period(fields)
            possible_subject_values = [
                value
                for label, values in fields.items()
                if _record_field_name(label) != "owner"
                if not _status_label(label)
                if normalize(label) not in temporal_labels
                for value in values
            ]
            exact_target_in_possible = any(
                assignment_target_identity(value) == target_identity
                for value in possible_subject_values
            )
            untyped_date_count = sum(
                _structured_date(value) is not None
                for values in fields.values()
                for value in values
            )
            assignment_shaped = has_owner_field or (
                temporal_context is not None
                and (
                    has_temporal_field
                    or has_possible_period
                    or _has_assignment_time_metadata(fields)
                    or (
                        exact_target_in_possible
                        and (untyped_date_count >= 2 or len(fields) >= 4)
                    )
                )
            )
            if assignment_shaped and any(
                assignment_target_identity(value) == target_identity
                for value in possible_subject_values
            ):
                relevant_tables.add(table_key)

        for record, row_index, sheet_name, table_key, fields in row_infos:
            if table_key not in relevant_tables:
                continue
            has_temporal_field = any(
                normalize(label) in temporal_labels for label in fields
            )
            has_owner_field = any(
                _record_field_name(label) == "owner" for label in fields
            )
            has_possible_period = _has_possible_assignment_period(fields)
            possible_subject_values = [
                value
                for label, values in fields.items()
                if _record_field_name(label) != "owner"
                if not _status_label(label)
                if normalize(label) not in temporal_labels
                for value in values
            ]
            exact_target_present = any(
                assignment_target_identity(value) == target_identity
                for value in possible_subject_values
            )
            untyped_date_count = sum(
                _structured_date(value) is not None
                for values in fields.values()
                for value in values
            )
            if (
                temporal_context is None
                and _has_assignment_time_metadata(fields)
                and exact_target_present
            ):
                owner_temporal_scope_required.append({
                    "evidence_id": record["evidence_id"],
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                    "reason": "assignment_period_requires_explicit_time_scope",
                })
            if temporal_context is not None:
                if not has_temporal_field and not (
                    (has_owner_field or has_possible_period)
                    and exact_target_present
                ) and not (
                    exact_target_present
                    and (untyped_date_count >= 2 or len(fields) >= 4)
                ):
                    continue
            elif not has_owner_field:
                continue
            subject_entries = [
                (label, values)
                for label, values in fields.items()
                if subject_label(label)
            ]
            identities = {
                assignment_target_identity(value)
                for _label, values in subject_entries
                for value in values
                if value.strip()
            }
            unknown_target_fields = [
                {"label": label, "values": list(values)}
                for label, values in fields.items()
                if not subject_label(label)
                if _record_field_name(label) != "owner"
                if not _status_label(label)
                if normalize(label) not in temporal_labels
                if any(
                    assignment_target_identity(value) == target_identity
                    for value in values
                )
            ]
            recognized_target_present = target_identity in identities
            if not recognized_target_present and not unknown_target_fields:
                continue
            malformed = (
                not subject_entries
                or len(subject_entries) != 1
                or any(
                    len(values) != 1 or not values[0].strip()
                    for _label, values in subject_entries
                )
                or len(identities) != 1
                or (
                    target_identity not in identities
                    and bool(unknown_target_fields)
                )
            )
            if malformed:
                temporal_subject_issues.append({
                    "evidence_id": record["evidence_id"],
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                    "subject_fields": [
                        {"label": label, "values": list(values)}
                        for label, values in subject_entries
                    ],
                    "unknown_target_fields": unknown_target_fields,
                })

    for record in records:
        locator = record["locator"]
        row_index = locator.get("row_index")
        sheet_name = locator.get("sheet_name")
        if not isinstance(row_index, int) or not isinstance(sheet_name, str):
            continue
        fields = _fields(record["text"])
        if not fields:
            continue

        subject_matches = []
        for label, values in fields.items():
            if _record_field_name(label) in requested_names or _status_label(label):
                continue
            if owner_lookup and not (
                _owner_subject_label(label)
                if temporal_context is not None
                else _preferred_subject_label(label)
            ):
                continue
            if len(values) != 1:
                continue
            value = values[0].strip()
            normalized_value = normalize(value)
            if (
                not normalized_value
                or normalized_value.isdecimal()
                or (
                    not owner_lookup
                    and len(normalized_value) < 3
                )
                or normalized_value not in question_normalized
            ):
                continue
            subject_matches.append({
                "label": label,
                "value": value,
                "preferred": _preferred_subject_label(label),
                "specificity": len(normalized_value),
            })
        if not subject_matches:
            continue
        subject_rows += 1
        subject = sorted(
            subject_matches,
            key=lambda item: (
                -int(item["preferred"]), -item["specificity"],
                normalize(item["label"]), normalize(item["value"]),
            ),
        )[0]
        if (
            temporal_context is not None
            and assignment_target_identity(subject["value"])
            != assignment_target_identity(temporal_context["target"])
        ):
            continue

        temporal = None
        if temporal_context is not None:
            assignment_status_fields = [
                {"label": label, "values": list(values)}
                for label, values in fields.items()
                if normalize(label) in {
                    normalize(alias)
                    for alias in ASSIGNMENT_STATUS_LABEL_ALIASES
                }
            ]
            if assignment_status_fields:
                temporal_status_invalid.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "fields": assignment_status_fields,
                    "reason": "assignment_status_semantics_not_supported",
                })
                continue
            combined_period_fields = [
                {"label": label, "values": list(values)}
                for label, values in fields.items()
                if normalize(label) in {
                    normalize(alias)
                    for alias in POSSIBLE_COMBINED_PERIOD_LABEL_ALIASES
                }
            ]
            if combined_period_fields:
                temporal_invalid.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "combined_period_fields": combined_period_fields,
                    "reason": "combined_assignment_period_not_supported",
                })
                continue
            start_entry, start_error = _single_temporal_entry(
                fields, TEMPORAL_START_LABEL_ALIASES
            )
            if start_error is not None:
                temporal_missing.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start": start_error,
                    "end": "not_checked",
                })
                continue
            assert start_entry is not None and as_of is not None
            start_date = _structured_date(start_entry["value"])
            if start_date is None:
                temporal_invalid.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start": start_entry["value"],
                    "end": "not_checked",
                })
                continue
            if start_date > as_of:
                exclusion = {
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start_date": start_date.isoformat(),
                    "end_date": None,
                    "reason": "starts_after_as_of",
                }
                temporal_excluded.append(exclusion)
                temporal_excluded_candidates.append({
                    "record": record,
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                    "subject": subject,
                    "start": start_entry,
                    "end": None,
                    **exclusion,
                })
                continue
            end_entry, end_error = _single_temporal_entry(
                fields, TEMPORAL_END_LABEL_ALIASES
            )
            if end_error is not None:
                temporal_missing.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start": None,
                    "end": end_error,
                })
                continue
            assert end_entry is not None
            end_date = _structured_date(end_entry["value"])
            if end_date is None or start_date > end_date:
                temporal_invalid.append({
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start": start_entry["value"],
                    "end": end_entry["value"],
                })
                continue
            temporal = {
                "start": start_entry,
                "end": end_entry,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "as_of": as_of.isoformat(),
            }
            if not start_date <= as_of <= end_date:
                exclusion = {
                    "evidence_id": record["evidence_id"],
                    "row_index": row_index,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "reason": "ends_before_as_of",
                }
                temporal_excluded.append(exclusion)
                temporal_excluded_candidates.append({
                    "record": record,
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                    "subject": subject,
                    "start": start_entry,
                    "end": end_entry,
                    **exclusion,
                })
                continue

        requested: dict[str, dict[str, str]] = {}
        row_invalid = False
        for item in plan_items:
            entries = _single_field_entries(fields, item["field_name"])
            if not entries:
                missing_fields.add(item["item_id"])
                row_invalid = True
                continue
            if len(entries) != 1 or entries[0]["cardinality"] != "1":
                ambiguous_fields.add(item["item_id"])
                row_invalid = True
                continue
            if owner_lookup and item["field_name"] == "owner":
                decoded_owner = _decode_json_string_literal(entries[0]["value"])
                has_control = any(
                    unicodedata.category(char) == "Cc" for char in decoded_owner
                )
                owner_value = _canonical_text(decoded_owner)
                entries[0]["value"] = owner_value
                owner_identity = normalize(owner_value)
                invalid_reason = (
                    "unsafe_control_or_instruction"
                    if has_control
                    or len(owner_value) > 160
                    or OWNER_INSTRUCTION_SURFACE.search(owner_value)
                    else "uncertain_or_alternative"
                    if OWNER_UNCERTAIN_SURFACE.search(owner_value)
                    else "negated_or_transition"
                    if OWNER_NEGATED_SURFACE.search(owner_value)
                    or OWNER_TRANSITION_SURFACE.search(owner_value)
                    else "incomplete_cardinality"
                    if re.search(
                        r"(?:ほか|他)\s*(?:数|複数|若干|"
                        r"[0-9〇零一二三四五六七八九十百]+)\s*(?:名|人)"
                        r"|[+＋]\s*(?:数|複数|若干|"
                        r"[0-9〇零一二三四五六七八九十百]+)\s*(?:名|人)"
                        r"|(?:を含む|含め)\s*(?:数|複数|若干|"
                        r"[0-9〇零一二三四五六七八九十百]+)\s*(?:名|人)"
                        r"|(?:ほか|他)\s*(?:担当者)?(?:未記載|未詳|不明|メンバー)\s*$"
                        r"|(?:ほか|他|など)(?:未詳|不明)?\s*$"
                        r"|(?:含む|含め(?:て)?).*?(?:計\s*)?"
                        r"(?:数|複数|若干|多数|[0-9〇零一二三四五六七八九十百]+)"
                        r"(?:名|人|担当者|チーム)?"
                        r"|(?:ら|等|など)\s*(?:数|複数|若干|多数|"
                        r"[0-9〇零一二三四五六七八九十百]+)\s*(?:名|人)?"
                        r"|(?:ほか|他)\s*多数|[+＋]\s*α|^複数\s*[（(]"
                        r"|(?:を含む|含め(?:て)?)\s*(?:チーム|メンバー)"
                        r"|(?:ほか|他)\s*若干\s*$"
                        r"|(?:と|および|及び|ならびに|並びに|ほか|他)\s*"
                        r"(?:チーム|メンバー|スタッフ|その他|誰か)\s*$"
                        r"|(?<![0-9a-z])with\s+"
                        r"(?:team|others|members?|staff)(?![0-9a-z])\s*$"
                        r"|(?<![0-9a-z])(?:and|with)\s+someone(?![0-9a-z])\s*$"
                        r"|\s(?:ら|等)\s*$"
                        r"|(?<![0-9a-z])(?:and|plus)\s+others(?![0-9a-z])"
                        r"|&\s*others(?![0-9a-z])|(?:,|\s)etc\.?(?:\s|$)"
                        r"|(?<![0-9a-z])et\s+al\.?(?![0-9a-z])",
                        owner_value,
                        re.IGNORECASE,
                    )
                    else "formula_unresolved"
                    if owner_value.lstrip("'’ ").startswith("=")
                    else "unknown_or_unassigned"
                    if not owner_identity
                    or OWNER_PLACEHOLDER_SURFACE.fullmatch(owner_identity)
                    else "non_person_scalar"
                    if owner_identity.isdecimal()
                    or owner_identity in {"true", "false", "yes", "no"}
                    or _structured_date(owner_value) is not None
                    or re.fullmatch(
                        r"(?:複数|数|[0-9〇零一二三四五六七八九十百]+)(?:名|人)",
                        owner_identity,
                    )
                    else None
                )
                if invalid_reason is None:
                    owner_components = [
                        component.strip()
                        for component in re.split(
                            r"\s*(?:[/／,、;；&+＋・|｜•:：]|\band\b)\s*"
                            r"|\s+[-―—–]\s+",
                            owner_value,
                            flags=re.IGNORECASE,
                        )
                    ]
                    owner_components.extend(re.findall(
                        r"[(（[【]\s*([^)）\]】]+?)\s*[)）\]】]",
                        owner_value,
                    ))
                    if len(owner_components) > 1:
                        for component in owner_components:
                            component_identity = normalize(component)
                            if (
                                not component_identity
                                or OWNER_PLACEHOLDER_SURFACE.fullmatch(
                                    component_identity
                                )
                                or component_identity in {
                                    "team", "teams", "member", "members", "staff",
                                    "someone", "others", "誰か", "ら", "等",
                                    "チーム", "メンバー", "スタッフ",
                                }
                            ):
                                invalid_reason = "unknown_or_unassigned"
                            elif component.lstrip("'’ ").startswith("="):
                                invalid_reason = "formula_unresolved"
                            elif (
                                component_identity.isdecimal()
                                or component_identity in {
                                    "true", "false", "yes", "no",
                                }
                                or _structured_date(component) is not None
                            ):
                                invalid_reason = "non_person_scalar"
                            if invalid_reason is not None:
                                break
                if invalid_reason is not None:
                    temporal_owner_invalid.append({
                        "evidence_id": record["evidence_id"],
                        "row_index": row_index,
                        "label": entries[0]["label"],
                        "value": owner_value,
                        "reason": invalid_reason,
                    })
                    row_invalid = True
                    continue
            requested[item["item_id"]] = entries[0]
        if row_invalid:
            continue

        status = None
        raw_status_entries = [
            (label, values)
            for label, values in fields.items()
            if _status_label(label)
        ]
        status_entries = [
            {"label": label, "value": values[0].strip()}
            for label, values in raw_status_entries
            if len(values) == 1 and values[0].strip()
        ]
        if require_final:
            if len(status_entries) != 1:
                nonfinal_statuses.add("missing_or_ambiguous")
                continue
            status = status_entries[0]
            normalized_status = normalize(status["value"])
            if (
                normalized_status not in {normalize(value) for value in FINAL_RECORD_STATUSES}
                or any(normalize(marker) in normalized_status for marker in NONFINAL_RECORD_STATUS_MARKERS)
            ):
                nonfinal_statuses.add(status["value"])
                continue
        elif len(status_entries) == 1:
            status = status_entries[0]

        candidates.append({
            "record": record,
            "sheet_name": sheet_name,
            "row_index": row_index,
            "subject": subject,
            "status": status,
            "requested": requested,
            "temporal": temporal,
        })
    candidates.sort(key=lambda item: (
        item["record"]["document_id"], item["record"]["relative_path"],
        item["sheet_name"], item["row_index"], item["record"]["evidence_id"],
    ))
    temporal_excluded_candidates.sort(key=lambda item: (
        item["record"]["document_id"], item["record"]["relative_path"],
        item["sheet_name"], item["row_index"], item["record"]["evidence_id"],
    ))
    return candidates, {
        "subject_rows": subject_rows,
        "missing_item_ids": sorted(missing_fields),
        "ambiguous_item_ids": sorted(ambiguous_fields),
        "nonfinal_statuses": sorted(nonfinal_statuses),
        "temporal_missing": temporal_missing,
        "temporal_invalid": temporal_invalid,
        "temporal_excluded": temporal_excluded,
        "temporal_subject_issues": temporal_subject_issues,
        "temporal_locator_issues": temporal_locator_issues,
        "temporal_owner_invalid": temporal_owner_invalid,
        "owner_temporal_scope_required": owner_temporal_scope_required,
        "owner_row_unit_issues": owner_row_unit_issues,
        "temporal_status_invalid": temporal_status_invalid,
    }, temporal_excluded_candidates


def _record_lookup_plan_surfaces(
    question: str,
    question_plan: Mapping[str, Any],
) -> list[dict[str, str]]:
    surfaces = [{"source": "question", "text": question}]
    raw_items = question_plan.get("items")
    if isinstance(raw_items, list):
        for index, raw in enumerate(raw_items, 1):
            if not isinstance(raw, Mapping):
                continue
            item_id = str(raw.get("item_id", f"item_{index}"))
            for key in ("label", "required_claim", "retrieval_query"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    surfaces.append({
                        "source": f"plan:{item_id}:{key}",
                        "text": value,
                    })
    answer_shape = question_plan.get("answer_shape")
    if isinstance(answer_shape, str) and answer_shape.strip():
        surfaces.append({"source": "plan:answer_shape", "text": answer_shape})
    return surfaces


def _unplanned_record_field_mentions(
    question: str,
    question_plan: Mapping[str, Any],
    candidate: Mapping[str, Any],
    plan_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    planned_fields = {item["field_name"] for item in plan_items}
    surfaces = _record_lookup_plan_surfaces(question, question_plan)
    fields = _fields(candidate["record"]["text"])
    missing = []
    for label in fields:
        if _status_label(label) or _preferred_subject_label(label):
            continue
        canonical_field = _record_field_name(label)
        if canonical_field in planned_fields:
            continue
        mentioned_by = [
            surface["source"] for surface in surfaces
            if _record_alias_mentioned(surface["text"], label)
        ]
        if mentioned_by:
            missing.append({
                "label": label,
                "canonical_field": canonical_field,
                "mentioned_by": mentioned_by,
            })
    return sorted(
        missing,
        key=lambda item: (normalize(item["label"]), item["label"]),
    )


def _cell_position(locator: Mapping[str, Any]) -> tuple[str | None, int | None]:
    cell = str(locator.get("cell", "")).strip()
    match = CELL_COORDINATE.match(cell)
    if match:
        return match.group("column").upper(), int(match.group("row"))
    column = str(locator.get("column", "")).strip().upper()
    row_index = locator.get("row_index")
    return (column or None), row_index if isinstance(row_index, int) else None


def _decode_json_string_literal(value: str) -> str:
    stripped = value.strip()
    if not (len(stripped) >= 2 and stripped.startswith('"') and stripped.endswith('"')):
        return value
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return value
    return decoded if isinstance(decoded, str) else value


def _formula_projection(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    formula = SAVED_VALUE_ANNOTATION.sub("", value.strip()).strip()
    if not formula or unicodedata.normalize("NFKC", formula[0]) != "=":
        return None

    operators = set("=+-*/^&%(),:<>!")
    output: list[str] = []
    pending_space = False
    last_kind: str | None = None

    def emit(text: str, kind: str) -> None:
        nonlocal pending_space, last_kind
        if (
            pending_space and output
            and last_kind != "operator" and kind != "operator"
        ):
            output.append(" ")
        output.append(text)
        pending_space = False
        last_kind = kind

    index = 0
    while index < len(formula):
        char = formula[index]
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            segment = [char]
            index += 1
            while index < len(formula):
                current = formula[index]
                segment.append(current)
                index += 1
                if current != quote:
                    continue
                if index < len(formula) and formula[index] == quote:
                    segment.append(formula[index])
                    index += 1
                    continue
                break
            emit("".join(segment), "operand")
            continue
        if char == "[":
            depth = 0
            segment = []
            while index < len(formula):
                current = formula[index]
                segment.append(current)
                index += 1
                if current == "[":
                    depth += 1
                elif current == "]":
                    depth -= 1
                    if depth == 0:
                        break
            emit("".join(segment), "operand")
            continue

        normalized = unicodedata.normalize("NFKC", char).casefold()
        for current in normalized:
            if current.isspace():
                pending_space = True
                continue
            emit(
                current,
                "operator" if current in operators else "operand",
            )
        index += 1
    return "".join(output)


def _raw_value_matches(observed: str, expected: str) -> bool:
    observed = _decode_json_string_literal(observed)
    expected = _decode_json_string_literal(expected)
    observed_formula = _formula_projection(observed)
    expected_formula = _formula_projection(expected)
    if observed_formula is not None and expected_formula is not None:
        return observed_formula == expected_formula
    observed_number = _decimal(observed)
    expected_number = _decimal(expected)
    if observed_number is not None and expected_number is not None:
        return observed_number == expected_number

    def strict_text(value: str) -> str:
        return _canonical_text(value).casefold()

    if strict_text(observed) == strict_text(expected):
        return True
    parsed_fields = _fields(observed)
    parsed_values = [
        value for values in parsed_fields.values() for value in values
    ]
    return (
        len(parsed_values) == 1
        and strict_text(parsed_values[0]) == strict_text(expected)
    )


def _raw_header_matches(observed: str, accepted_labels: set[str]) -> bool:
    observed = _decode_json_string_literal(observed)
    if normalize(observed) in accepted_labels:
        return True
    parsed_fields = _fields(observed)
    parsed_labels_and_values = [
        *parsed_fields.keys(),
        *(value for values in parsed_fields.values() for value in values),
    ]
    return any(normalize(value) in accepted_labels for value in parsed_labels_and_values)


def _record_field_lineage(
    traversal: Mapping[str, Any],
    evidence_universe: list[dict[str, Any]],
    candidate: Mapping[str, Any],
    label: str,
    value: str,
    field_name: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row_record = candidate["record"]
    row_id = row_record["evidence_id"]
    record_by_id = {record["evidence_id"]: record for record in evidence_universe}
    paths = traversal["paths"]
    lineage_edges = sorted([
        edge for edge in traversal["edge_by_id"].values()
        if edge.get("from_node_id") == row_id
        and edge.get("relation_type") == "derived_from"
        and edge.get("relation_class") == "lineage"
        and edge.get("status") == "verified"
        and edge.get("basis_kind") == "explicit"
        and str(edge.get("to_node_id", "")) in paths
    ], key=lambda edge: edge["relation_id"])

    accepted_labels = {normalize(label)}
    if field_name is not None:
        accepted_labels.update(
            normalize(alias) for alias in RECORD_LOOKUP_FIELD_ALIASES[field_name]
        )
    matching_headers = []
    for edge in lineage_edges:
        target = record_by_id.get(str(edge.get("to_node_id", "")))
        if target is None or target["document_id"] != row_record["document_id"]:
            continue
        locator = target["locator"]
        if locator.get("sheet_name") != candidate["sheet_name"]:
            continue
        column, row_index = _cell_position(locator)
        if (
            column is None or row_index is None
            or row_index >= candidate["row_index"]
            or not _raw_header_matches(target["text"], accepted_labels)
        ):
            continue
        matching_headers.append((target, edge, column, row_index))
    nearest_header_row = max(
        (item[3] for item in matching_headers),
        default=None,
    )
    header_candidates = [
        (target, edge, column)
        for target, edge, column, row_index in matching_headers
        if row_index == nearest_header_row
    ]

    pairs = []
    coordinate_issues = []
    for header, header_edge, column in header_candidates:
        _header_column, header_row = _cell_position(header["locator"])
        header_coordinate_records = [
            record for record in evidence_universe
            if record["document_id"] == row_record["document_id"]
            and record["relative_path"] == row_record["relative_path"]
            and record["locator"].get("sheet_name") == candidate["sheet_name"]
            and _cell_position(record["locator"]) == (column, header_row)
        ]
        value_coordinate_records = [
            record for record in evidence_universe
            if record["document_id"] == row_record["document_id"]
            and record["relative_path"] == row_record["relative_path"]
            and record["locator"].get("sheet_name") == candidate["sheet_name"]
            and _cell_position(record["locator"])
            == (column, candidate["row_index"])
        ]
        if (
            len(header_coordinate_records) != 1
            or len(value_coordinate_records) != 1
        ):
            coordinate_issues.append({
                "column": column,
                "header_row": header_row,
                "value_row": candidate["row_index"],
                "header_evidence_ids": sorted(
                    record["evidence_id"] for record in header_coordinate_records
                ),
                "value_evidence_ids": sorted(
                    record["evidence_id"] for record in value_coordinate_records
                ),
            })
            continue
        value_candidates = []
        for edge in lineage_edges:
            target = record_by_id.get(str(edge.get("to_node_id", "")))
            if target is None or target["document_id"] != row_record["document_id"]:
                continue
            locator = target["locator"]
            target_column, target_row = _cell_position(locator)
            if (
                locator.get("sheet_name") == candidate["sheet_name"]
                and target_column == column
                and target_row == candidate["row_index"]
                and _raw_value_matches(target["text"], value)
            ):
                value_candidates.append((target, edge))
        if len(value_candidates) == 1:
            value_record, value_edge = value_candidates[0]
            pairs.append((header, header_edge, value_record, value_edge, column))
        elif value_candidates:
            pairs.extend(
                (header, header_edge, value_record, value_edge, column)
                for value_record, value_edge in value_candidates
            )
    if coordinate_issues:
        return None, {
            "code": "record_lookup_coordinate_cardinality_invalid",
            "detail": {
                "row_evidence_id": row_id,
                "field_name": field_name,
                "label": label,
                "coordinates": coordinate_issues,
            },
        }
    if len(pairs) != 1:
        return None, {
            "code": "record_lookup_field_lineage_invalid",
            "detail": {
                "row_evidence_id": row_id,
                "field_name": field_name,
                "label": label,
                "header_candidates": [item[0]["evidence_id"] for item in header_candidates],
                "lineage_pairs": [
                    {"header_id": item[0]["evidence_id"], "value_id": item[2]["evidence_id"]}
                    for item in pairs
                ],
            },
        }
    header, header_edge, value_record, value_edge, column = pairs[0]
    return {
        "field_name": field_name,
        "label": label,
        "row_value": value,
        "value": _decode_json_string_literal(value_record["text"]),
        "column": column,
        "row_search_unit_id": row_id,
        "header_evidence_id": header["evidence_id"],
        "value_evidence_id": value_record["evidence_id"],
        "header_relation_id": header_edge["relation_id"],
        "value_relation_id": value_edge["relation_id"],
        "source_evidence_ids": [header["evidence_id"], value_record["evidence_id"]],
        "relation_ids": [header_edge["relation_id"], value_edge["relation_id"]],
    }, None


def _temporal_exclusion_lineages(
    traversal: Mapping[str, Any],
    evidence_universe: list[dict[str, Any]],
    excluded_candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind every negative period proof used to establish candidate uniqueness."""
    lineages = []
    failures = []
    for candidate in excluded_candidates:
        subject_lineage, subject_error = _record_field_lineage(
            traversal,
            evidence_universe,
            candidate,
            candidate["subject"]["label"],
            candidate["subject"]["value"],
            None,
        )
        start_lineage, start_error = _record_field_lineage(
            traversal,
            evidence_universe,
            candidate,
            candidate["start"]["label"],
            candidate["start"]["value"],
            None,
        )
        end_lineage = None
        end_error = None
        if candidate["end"] is not None:
            end_lineage, end_error = _record_field_lineage(
                traversal,
                evidence_universe,
                candidate,
                candidate["end"]["label"],
                candidate["end"]["value"],
                None,
            )
        if subject_error is not None or start_error is not None or end_error is not None:
            failures.append({
                "record_evidence_id": candidate["record"]["evidence_id"],
                "subject": subject_error,
                "start": start_error,
                "end": end_error,
            })
            continue
        component_lineages = [
            subject_lineage,
            start_lineage,
            *([end_lineage] if end_lineage is not None else []),
        ]
        lineages.append({
            "record_evidence_id": candidate["record"]["evidence_id"],
            "row_index": candidate["row_index"],
            "reason": candidate["reason"],
            "start_date": candidate["start_date"],
            "end_date": candidate["end_date"],
            "subject": subject_lineage,
            "start": start_lineage,
            "end": end_lineage,
            "source_evidence_ids": list(dict.fromkeys(
                evidence_id
                for lineage in component_lineages
                for evidence_id in lineage["source_evidence_ids"]
            )),
            "relation_ids": list(dict.fromkeys(
                relation_id
                for lineage in component_lineages
                for relation_id in lineage["relation_ids"]
            )),
        })
    return lineages, failures


def _saved_number(value: str) -> Decimal | None:
    match = SAVED_VALUE.search(unicodedata.normalize("NFKC", value))
    return _decimal(match.group("value")) if match else None


def _candidate_rows(records: list[dict[str, Any]], question: str, scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for record in records:
        locator = record["locator"]
        row_index = locator.get("row_index")
        sheet_name = locator.get("sheet_name")
        if not isinstance(row_index, int) or not isinstance(sheet_name, str):
            continue
        if not _scope_matches(record["relative_path"], scope):
            continue
        for label, values in _fields(record["text"]).items():
            for value in values:
                formula = SUM_FORMULA.search(value)
                saved = _saved_number(value)
                if not formula or saved is None:
                    continue
                start_col = formula.group("start_col").upper()
                end_col = formula.group("end_col").upper()
                if start_col != end_col:
                    continue
                candidates.append({
                    "candidate_id": "agg_" + stable_hash({
                        "evidence_id": record["evidence_id"], "label": label,
                        "formula": formula.group(0), "value": _decimal_text(saved),
                    })[:24],
                    "record": record,
                    "label": label,
                    "score": _subject_score(question, label),
                    "formula": formula.group(0).replace(" ", ""),
                    "column": start_col,
                    "start_row": int(formula.group("start_row")),
                    "end_row": int(formula.group("end_row")),
                    "saved_value": saved,
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                })
    return candidates


def _cell_companions(records: list[dict[str, Any]], candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    cell = f"{candidate['column']}{candidate['row_index']}"
    result = []
    for record in records:
        locator = record["locator"]
        if (
            record["document_id"] == candidate["record"]["document_id"]
            and locator.get("sheet_name") == candidate["sheet_name"]
            and str(locator.get("cell", "")).upper() == cell
        ):
            result.append(record)
    return result


def _row_projection(records: list[dict[str, Any]], candidate: Mapping[str, Any]) -> dict[str, Any]:
    by_row: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        locator = record["locator"]
        row_index = locator.get("row_index")
        if (
            record["document_id"] == candidate["record"]["document_id"]
            and locator.get("sheet_name") == candidate["sheet_name"]
            and isinstance(row_index, int)
            and candidate["start_row"] <= row_index <= candidate["end_row"]
        ):
            by_row.setdefault(row_index, []).append(record)

    expected_rows = list(range(candidate["start_row"], candidate["end_row"] + 1))
    missing_rows = [row for row in expected_rows if row not in by_row]
    values: dict[int, Decimal] = {}
    conflicts: list[int] = []
    validation_ids: list[str] = []
    nonzero_ids: list[str] = []
    label = str(candidate["label"])
    for row in expected_rows:
        row_records = by_row.get(row, [])
        validation_ids.extend(record["evidence_id"] for record in row_records)
        observed: list[tuple[Decimal, str]] = []
        for record in row_records:
            for value in _fields(record["text"]).get(label, []):
                if value.startswith("="):
                    continue
                parsed = _decimal(value)
                if parsed is not None:
                    observed.append((parsed, record["evidence_id"]))
        distinct = {value for value, _ in observed}
        if len(distinct) > 1:
            conflicts.append(row)
            continue
        if distinct:
            values[row] = next(iter(distinct))
            nonzero_ids.extend(evidence_id for _, evidence_id in observed)
        else:
            values[row] = Decimal(0)

    total = sum(values.values(), Decimal(0)) if not missing_rows and not conflicts else None
    return {
        "complete": not missing_rows and not conflicts,
        "expected_row_count": len(expected_rows),
        "covered_row_count": len(expected_rows) - len(missing_rows),
        "missing_rows": missing_rows,
        "conflicting_rows": conflicts,
        "recomputed_value": _decimal_text(total) if total is not None else None,
        "validation_evidence_ids": list(dict.fromkeys(validation_ids)),
        "nonzero_evidence_ids": list(dict.fromkeys(nonzero_ids)),
    }


def _reference_records(records: list[dict[str, Any]], candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected_cell = f"{candidate['column']}{candidate['row_index']}".upper()
    result = []
    for record in records:
        if record["document_id"] != candidate["record"]["document_id"]:
            continue
        fields = _fields(record["text"])
        if not any(_subject_score(str(candidate["label"]), label) >= 0.8 for label in fields):
            continue
        for values in fields.values():
            for value in values:
                reference = CELL_REFERENCE.search(unicodedata.normalize("NFKC", value))
                saved = _saved_number(value)
                if not reference or saved != candidate["saved_value"]:
                    continue
                referenced_cell = f"{reference.group('column')}{reference.group('row')}".upper()
                referenced_sheet = reference.group("quoted_sheet") or reference.group("sheet")
                if referenced_cell != expected_cell:
                    continue
                if referenced_sheet and normalize(referenced_sheet) != normalize(candidate["sheet_name"]):
                    continue
                result.append(record)
                break
    return result


def _prepare_stored_graph_traversal(
    source_graph: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Traverse the validated persistent provenance graph from each Document.

    ``contains`` is followed in its stored direction.  ``derived_from`` points
    from a SearchUnit to its source Evidence, so provenance discovery follows
    that relation in reverse.  The returned predecessor tree is canonical and
    later binds every Evidence used by the question overlay to concrete stored
    relation IDs.
    """
    if not isinstance(source_graph, Mapping):
        return None, {"code": "source_graph_missing", "detail": "Stored Graph is missing."}
    nodes = source_graph.get("nodes")
    edges = source_graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None, {"code": "source_graph_shape_invalid", "detail": "Stored Graph nodes/edges are invalid."}

    node_by_id: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        if not isinstance(raw, Mapping):
            return None, {"code": "source_graph_node_invalid", "detail": "Stored Graph Node is not an object."}
        node_id = str(raw.get("node_id", "")).strip()
        if not node_id or node_id in node_by_id:
            return None, {"code": "source_graph_node_id_invalid", "detail": node_id}
        node = dict(raw)
        if node.get("node_type") not in {"document", "evidence"}:
            return None, {"code": "source_graph_node_type_invalid", "detail": node_id}
        node_by_id[node_id] = node

    edge_by_id: dict[str, dict[str, Any]] = {}
    transitions: dict[str, list[tuple[str, str, str]]] = {}
    for raw in edges:
        if not isinstance(raw, Mapping):
            return None, {"code": "source_graph_edge_invalid", "detail": "Stored Graph Edge is not an object."}
        relation_id = str(raw.get("relation_id", "")).strip()
        source = str(raw.get("from_node_id", "")).strip()
        target = str(raw.get("to_node_id", "")).strip()
        if (
            not relation_id or relation_id in edge_by_id
            or source not in node_by_id or target not in node_by_id
        ):
            return None, {"code": "source_graph_edge_binding_invalid", "detail": relation_id}
        edge = dict(raw)
        edge_by_id[relation_id] = edge
        if edge.get("status") != "verified" or edge.get("basis_kind") != "explicit":
            continue
        if (
            edge.get("relation_type") == "contains"
            and edge.get("relation_class") == "structural"
        ):
            transitions.setdefault(source, []).append((target, relation_id, "forward"))
        elif (
            edge.get("relation_type") == "derived_from"
            and edge.get("relation_class") == "lineage"
        ):
            transitions.setdefault(target, []).append((source, relation_id, "reverse"))
    for values in transitions.values():
        values.sort(key=lambda item: (item[1], item[0], item[2]))

    root_ids = sorted(
        node_id for node_id, node in node_by_id.items()
        if node.get("node_type") == "document"
    )
    if not root_ids:
        return None, {"code": "source_graph_document_missing", "detail": "No Document root exists."}

    records_by_id = {record["evidence_id"]: record for record in records}
    claimed_eligible = source_graph.get("eligible_evidence_ids")
    if isinstance(claimed_eligible, list):
        eligible_ids = {str(value) for value in claimed_eligible}
    else:
        eligible_ids = {
            node_id for node_id, node in node_by_id.items()
            if node.get("node_type") == "evidence"
            and node.get("status") in {"observed", "verified"}
        }
    if eligible_ids != set(records_by_id):
        return None, {
            "code": "source_graph_evidence_universe_mismatch",
            "detail": {
                "missing_records": sorted(eligible_ids - set(records_by_id))[:8],
                "unbound_records": sorted(set(records_by_id) - eligible_ids)[:8],
            },
        }

    paths: dict[str, dict[str, Any]] = {}
    for root_id in root_ids:
        predecessor: dict[str, tuple[str, str, str] | None] = {root_id: None}
        queue: deque[str] = deque([root_id])
        while queue:
            current = queue.popleft()
            for target, relation_id, direction in transitions.get(current, []):
                if target in predecessor:
                    continue
                predecessor[target] = (current, relation_id, direction)
                queue.append(target)
        for evidence_id, record in records_by_id.items():
            if record["document_id"] != root_id or evidence_id not in predecessor:
                continue
            node_ids = [evidence_id]
            relation_ids: list[str] = []
            directions: list[str] = []
            cursor = evidence_id
            while cursor != root_id:
                step = predecessor.get(cursor)
                if step is None:
                    break
                parent, relation_id, direction = step
                relation_ids.append(relation_id)
                directions.append(direction)
                node_ids.append(parent)
                cursor = parent
            if cursor != root_id:
                continue
            paths[evidence_id] = {
                "evidence_id": evidence_id,
                "root_document_id": root_id,
                "node_ids": list(reversed(node_ids)),
                "relation_ids": list(reversed(relation_ids)),
                "directions": list(reversed(directions)),
            }

    unreachable = sorted(set(records_by_id) - set(paths))
    return {
        "source_graph": dict(source_graph),
        "node_by_id": node_by_id,
        "edge_by_id": edge_by_id,
        "paths": paths,
        "unreachable_evidence_ids": unreachable,
    }, None


def _unreachable_temporal_target_rows(
    records: list[dict[str, Any]],
    traversal: Mapping[str, Any],
    target: str,
) -> list[dict[str, Any]]:
    """Find target rows that graph traversal would otherwise silently discard."""
    unreachable = set(traversal.get("unreachable_evidence_ids", []))
    matches = []
    for issue in _orphan_owner_row_units(records, target):
        raw_ids = set(issue.get("target_evidence_ids", []))
        raw_ids.update(issue.get("owner_evidence_ids", []))
        raw_ids.update(issue.get("period_evidence_ids", []))
        if raw_ids & unreachable:
            matches.append({
                "evidence_id": sorted(raw_ids & unreachable)[0],
                "locator": {
                    "sheet_name": issue.get("sheet_name"),
                    "row_index": issue.get("row_index"),
                },
                "target_fields": [],
                "reason": "raw_assignment_row_unreachable",
            })
    for record in records:
        if record["evidence_id"] not in unreachable:
            continue
        locator = record["locator"]
        fields = _fields(record["text"])
        temporal_labels = {
            normalize(alias)
            for alias in (
                *TEMPORAL_START_LABEL_ALIASES,
                *TEMPORAL_END_LABEL_ALIASES,
            )
        }
        has_temporal_field = any(
            normalize(label) in temporal_labels for label in fields
        )
        has_owner_field = any(
            _record_field_name(label) == "owner" for label in fields
        )
        if not (has_temporal_field or has_owner_field):
            if _coordinate_less_target_row_like(record["text"], target):
                matches.append({
                    "evidence_id": record["evidence_id"],
                    "locator": dict(locator),
                    "target_fields": [],
                    "reason": "coordinate_less_target_row_unreachable",
                })
            else:
                unknown_target_fields = [
                    {"label": label, "value": value}
                    for label, values in fields.items()
                    for value in values
                    if assignment_target_identity(value)
                    == assignment_target_identity(target)
                ]
                structured_date_count = sum(
                    _structured_date(value) is not None
                    for values in fields.values()
                    for value in values
                )
                if unknown_target_fields and (
                    structured_date_count >= 2 or len(fields) >= 4
                ):
                    matches.append({
                        "evidence_id": record["evidence_id"],
                        "locator": dict(locator),
                        "target_fields": unknown_target_fields,
                        "reason": "unknown_assignment_schema_unreachable",
                    })
            continue
        target_fields = [
            {"label": label, "value": value}
            for label, values in fields.items()
            if _record_field_name(label) != "owner"
            if not _status_label(label)
            if normalize(label) not in temporal_labels
            for value in values
            if assignment_target_identity(value)
            == assignment_target_identity(target)
        ]
        if target_fields:
            matches.append({
                "evidence_id": record["evidence_id"],
                "locator": dict(locator),
                "target_fields": target_fields,
            })
    return sorted(
        matches,
        key=lambda item: (
            canonical_json(item["locator"]), item["evidence_id"],
        ),
    )


def _structured_aggregate_lineage(
    traversal: Mapping[str, Any],
    records: list[dict[str, Any]],
    evidence_universe: list[dict[str, Any]],
    candidate: Mapping[str, Any],
    companions: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    """Require the stored lineage that gives table text its aggregate meaning."""
    record_by_id = {record["evidence_id"]: record for record in records}
    edge_by_id = traversal["edge_by_id"]

    def lineage_from(evidence_id: str) -> list[dict[str, Any]]:
        return sorted([
            edge for edge in edge_by_id.values()
            if edge.get("from_node_id") == evidence_id
            and edge.get("relation_type") == "derived_from"
            and edge.get("relation_class") == "lineage"
            and edge.get("status") == "verified"
            and edge.get("basis_kind") == "explicit"
        ], key=lambda edge: edge["relation_id"])

    candidate_id = candidate["record"]["evidence_id"]
    candidate_edges = lineage_from(candidate_id)
    candidate_targets = {
        edge["to_node_id"]: edge for edge in candidate_edges
    }
    expected_cell = f"{candidate['column']}{candidate['row_index']}".upper()
    formula_records = [
        record for record in companions
        if re.sub(r"\s+", "", unicodedata.normalize("NFKC", record["text"]).upper())
        == re.sub(r"\s+", "", unicodedata.normalize("NFKC", candidate["formula"]).upper())
    ]
    saved_records = [
        record for record in companions
        if _decimal(record["text"]) == candidate["saved_value"]
    ]
    if len(formula_records) != 1 or len(saved_records) != 1:
        return None, [], {
            "code": "aggregate_source_cardinality_invalid",
            "detail": {
                "formula_sources": [record["evidence_id"] for record in formula_records],
                "saved_sources": [record["evidence_id"] for record in saved_records],
            },
        }
    formula_id = formula_records[0]["evidence_id"]
    saved_id = saved_records[0]["evidence_id"]
    if formula_id not in candidate_targets or saved_id not in candidate_targets:
        return None, [], {
            "code": "aggregate_source_lineage_missing",
            "detail": {"formula_id": formula_id, "saved_value_id": saved_id},
        }

    header_cell = f"{candidate['column']}1".upper()
    header_records = []
    for target_id in candidate_targets:
        record = record_by_id.get(target_id)
        if record is None:
            continue
        locator = record["locator"]
        same_location = (
            record["document_id"] == candidate["record"]["document_id"]
            and locator.get("sheet_name") == candidate["sheet_name"]
            and (
                str(locator.get("cell", "")).upper() == header_cell
                or locator.get("row_index") == 1
            )
        )
        if same_location and normalize(candidate["label"]) in normalize(record["text"]):
            header_records.append(record)
    if len(header_records) != 1:
        return None, [], {
            "code": "target_header_lineage_invalid",
            "detail": [record["evidence_id"] for record in header_records],
        }
    header_id = header_records[0]["evidence_id"]

    relation_ids = {
        candidate_targets[formula_id]["relation_id"],
        candidate_targets[saved_id]["relation_id"],
        candidate_targets[header_id]["relation_id"],
    }
    source_evidence_ids = {formula_id, saved_id, header_id}
    row_bindings = []
    row_records = []
    for row_index in range(candidate["start_row"], candidate["end_row"] + 1):
        location_records = [
            record for record in records
            if record["document_id"] == candidate["record"]["document_id"]
            and record["locator"].get("sheet_name") == candidate["sheet_name"]
            and record["locator"].get("row_index") == row_index
        ]
        linked = []
        for record in location_records:
            edges_to_header = [
                edge for edge in lineage_from(record["evidence_id"])
                if edge["to_node_id"] == header_id
            ]
            if edges_to_header:
                linked.append((record, edges_to_header[0]))
        if len(linked) != 1:
            return None, [], {
                "code": "range_row_lineage_cardinality_invalid",
                "detail": {
                    "row_index": row_index,
                    "linked_evidence_ids": [record["evidence_id"] for record, _ in linked],
                },
            }
        row_record, header_edge = linked[0]
        row_records.append(row_record)
        relation_ids.add(header_edge["relation_id"])

        field_values = [
            value for value in _fields(row_record["text"]).get(str(candidate["label"]), [])
            if value and not value.startswith("=")
        ]
        parsed_values = [_decimal(value) for value in field_values]
        if any(value is None for value in parsed_values):
            return None, [], {
                "code": "range_row_value_invalid",
                "detail": {"row_index": row_index, "values": field_values},
            }
        distinct_values = {value for value in parsed_values if value is not None}
        if len(distinct_values) > 1:
            return None, [], {
                "code": "range_row_value_conflict",
                "detail": {"row_index": row_index, "values": field_values},
            }

        target_cell = f"{candidate['column']}{row_index}".upper()
        cell_records = [
            record for record in evidence_universe
            if record["document_id"] == candidate["record"]["document_id"]
            and record["locator"].get("sheet_name") == candidate["sheet_name"]
            and str(record["locator"].get("cell", "")).upper() == target_cell
        ]
        target_sources = []
        for edge in lineage_from(row_record["evidence_id"]):
            target_record = record_by_id.get(edge["to_node_id"])
            if target_record is None:
                continue
            locator = target_record["locator"]
            if (
                target_record["document_id"] == candidate["record"]["document_id"]
                and locator.get("sheet_name") == candidate["sheet_name"]
                and str(locator.get("cell", "")).upper() == target_cell
            ):
                target_sources.append((target_record, edge))
        value = next(iter(distinct_values)) if distinct_values else Decimal(0)
        if distinct_values:
            source_ids = [record["evidence_id"] for record, _ in target_sources]
            cell_ids = [record["evidence_id"] for record in cell_records]
            if (
                len(cell_records) != 1
                or len(target_sources) != 1
                or source_ids != cell_ids
                or _decimal(cell_records[0]["text"]) != value
            ):
                return None, [], {
                    "code": "range_row_source_value_mismatch",
                    "detail": {
                        "row_index": row_index,
                        "row_value": _decimal_text(value),
                        "cell_ids": cell_ids,
                        "source_ids": source_ids,
                    },
                }
        elif cell_records or target_sources:
            return None, [], {
                "code": "blank_row_has_target_source",
                "detail": {
                    "row_index": row_index,
                    "cell_ids": [record["evidence_id"] for record in cell_records],
                    "source_ids": [record["evidence_id"] for record, _ in target_sources],
                },
            }

        target_id = None
        target_relation_id = None
        if target_sources:
            target_record, target_edge = target_sources[0]
            target_id = target_record["evidence_id"]
            target_relation_id = target_edge["relation_id"]
            source_evidence_ids.add(target_id)
            relation_ids.add(target_relation_id)
        row_bindings.append({
            "row_index": row_index,
            "search_unit_id": row_record["evidence_id"],
            "header_relation_id": header_edge["relation_id"],
            "value": _decimal_text(value),
            "target_cell_id": target_id,
            "target_cell_relation_id": target_relation_id,
        })

    lineage = {
        "aggregate_search_unit_id": candidate_id,
        "target_header_id": header_id,
        "formula_id": formula_id,
        "saved_value_id": saved_id,
        "expected_cell": expected_cell,
        "row_bindings": row_bindings,
        "source_evidence_ids": sorted(source_evidence_ids),
        "relation_ids": sorted(relation_ids),
    }
    return lineage, row_records, None


def _stored_graph_binding(
    traversal: Mapping[str, Any],
    required_evidence_ids: list[str],
    structured_lineage: Mapping[str, Any] | None = None,
    *,
    record_lookup_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_graph = traversal["source_graph"]
    node_by_id = traversal["node_by_id"]
    edge_by_id = traversal["edge_by_id"]
    paths = [traversal["paths"][evidence_id] for evidence_id in required_evidence_ids]
    relation_ids = {
        relation_id for path in paths for relation_id in path["relation_ids"]
    }
    if structured_lineage is not None:
        relation_ids.update(structured_lineage.get("relation_ids", []))
    if record_lookup_lineage is not None:
        relation_ids.update(record_lookup_lineage.get("relation_ids", []))
    relation_ids = sorted(relation_ids)
    node_ids = {node_id for path in paths for node_id in path["node_ids"]}
    for relation_id in relation_ids:
        edge = edge_by_id[relation_id]
        node_ids.add(edge["from_node_id"])
        node_ids.add(edge["to_node_id"])
    node_ids = sorted(node_ids)
    body = {
        "binding_version": STORED_GRAPH_BINDING_VERSION,
        "graph_sha256": source_graph.get("graph_sha256"),
        "partition_sha256": source_graph.get("partition_sha256"),
        "eligible_evidence_set_sha256": source_graph.get(
            "eligible_evidence_set_sha256"
        ),
        "required_evidence_ids": list(required_evidence_ids),
        "evidence_paths": paths,
        "traversed_node_ids": node_ids,
        "traversed_relation_ids": relation_ids,
        "traversed_node_hashes": [
            {"node_id": node_id, "record_sha256": node_by_id[node_id].get("record_sha256")}
            for node_id in node_ids
        ],
        "traversed_edge_hashes": [
            {
                "relation_id": relation_id,
                "record_sha256": edge_by_id[relation_id].get("record_sha256"),
            }
            for relation_id in relation_ids
        ],
        "structured_aggregate_lineage": (
            dict(structured_lineage) if structured_lineage is not None else None
        ),
    }
    if record_lookup_lineage is not None:
        body["structured_record_lookup_lineage"] = dict(record_lookup_lineage)
    return {**body, "traversal_sha256": stable_hash(body)}


def _artifact_body(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: artifact[key]
        for key in artifact
        if key not in {"artifact_hash", "artifact_id"}
    }


def _finish(body: dict[str, Any]) -> dict[str, Any]:
    artifact_hash = stable_hash(body)
    return {
        **body,
        "artifact_id": f"qeg_{artifact_hash[:24]}",
        "artifact_hash": artifact_hash,
    }


def _build_record_lookup_graph(
    base: dict[str, Any],
    question: str,
    records: list[dict[str, Any]],
    evidence_universe: list[dict[str, Any]],
    stored_traversal: Mapping[str, Any] | None,
    plan_items: list[dict[str, Any]],
    question_plan: Mapping[str, Any],
    temporal_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if stored_traversal is None:
        return _finish({
            **base,
            "status": "hold",
            "reason": "stored_graph_required",
            "audit": [{
                "check": "stored_graph_binding",
                "status": "fail",
                "details": "record_lookup requires a validated stored Graph",
            }],
        })

    require_final = bool(FINAL_RECORD_SURFACE.search(
        _record_status_scope_text(
            question, plan_items, temporal_context, records
        )
    ))
    candidates, diagnostics, excluded_candidates = _record_lookup_candidates(
        records, question, plan_items, require_final, temporal_context
    )
    owner_lookup = any(item["field_name"] == "owner" for item in plan_items)
    if owner_lookup and diagnostics["owner_row_unit_issues"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_owner_row_unit_missing",
            "audit": [{
                "check": "assignment_row_completeness",
                "status": "fail",
                "details": diagnostics["owner_row_unit_issues"],
            }],
        })
    if owner_lookup and diagnostics["temporal_locator_issues"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": (
                "record_lookup_temporal_locator_unsupported"
                if temporal_context is not None
                else "record_lookup_owner_locator_unsupported"
            ),
            "audit": [{
                "check": "assignment_row_locator",
                "status": "fail",
                "details": diagnostics["temporal_locator_issues"],
            }],
        })
    if (
        owner_lookup
        and temporal_context is None
        and diagnostics["owner_temporal_scope_required"]
    ):
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_owner_time_scope_required",
            "audit": [{
                "check": "assignment_time_scope",
                "status": "fail",
                "details": diagnostics["owner_temporal_scope_required"],
            }],
        })
    if owner_lookup and diagnostics["temporal_subject_issues"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": (
                "record_lookup_temporal_subject_invalid"
                if temporal_context is not None
                else "record_lookup_owner_subject_invalid"
            ),
            "audit": [{
                "check": "assignment_subject_identity",
                "status": "fail",
                "details": diagnostics["temporal_subject_issues"],
            }],
        })
    if owner_lookup and diagnostics["temporal_status_invalid"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": (
                "record_lookup_temporal_status_invalid"
                if temporal_context is not None
                else "record_lookup_owner_status_invalid"
            ),
            "audit": [{
                "check": "assignment_status",
                "status": "fail",
                "details": diagnostics["temporal_status_invalid"],
            }],
        })
    if temporal_context is not None and diagnostics["temporal_missing"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_temporal_evidence_missing",
            "audit": [{
                "check": "assignment_period_evidence",
                "status": "fail",
                "details": diagnostics["temporal_missing"],
            }],
        })
    if temporal_context is not None and diagnostics["temporal_invalid"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_temporal_period_invalid",
            "audit": [{
                "check": "assignment_period_validity",
                "status": "fail",
                "details": diagnostics["temporal_invalid"],
            }],
        })
    if owner_lookup and diagnostics["temporal_owner_invalid"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": (
                "record_lookup_temporal_owner_invalid"
                if temporal_context is not None
                else "record_lookup_owner_invalid"
            ),
            "audit": [{
                "check": "assignment_owner_value",
                "status": "fail",
                "details": diagnostics["temporal_owner_invalid"],
            }],
        })
    temporal_exclusion_lineages: list[dict[str, Any]] = []
    if temporal_context is not None:
        temporal_exclusion_lineages, exclusion_lineage_failures = (
            _temporal_exclusion_lineages(
                stored_traversal,
                evidence_universe,
                excluded_candidates,
            )
        )
        if exclusion_lineage_failures:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_lineage_failed",
                "audit": [{
                    "check": "stored_graph_assignment_exclusion_lineage",
                    "status": "fail",
                    "details": exclusion_lineage_failures,
                }],
            })
    if temporal_context is not None and diagnostics["ambiguous_item_ids"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_field_ambiguous",
            "audit": [{
                "check": "assignment_candidate_field_coverage",
                "status": "fail",
                "details": diagnostics,
            }],
        })
    if temporal_context is not None and diagnostics["missing_item_ids"]:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_field_missing",
            "audit": [{
                "check": "assignment_candidate_field_coverage",
                "status": "fail",
                "details": diagnostics,
            }],
        })
    if not candidates:
        if temporal_context is not None and diagnostics["temporal_excluded"]:
            reason = "record_lookup_temporal_not_found"
        elif diagnostics["ambiguous_item_ids"]:
            reason = "record_lookup_field_ambiguous"
        elif require_final and diagnostics["nonfinal_statuses"]:
            reason = "record_lookup_final_status_not_found"
        elif diagnostics["missing_item_ids"]:
            reason = "record_lookup_field_missing"
        else:
            reason = "record_lookup_subject_not_found"
        return _finish({
            **base,
            "status": "hold",
            "reason": reason,
            "audit": [{
                "check": "record_candidate_resolution",
                "status": "fail",
                "details": diagnostics,
            }],
        })
    if len(candidates) != 1:
        return _finish({
            **base,
            "status": "hold",
            "reason": (
                "record_lookup_temporal_candidate_ambiguous"
                if temporal_context is not None
                else "record_lookup_candidate_ambiguous"
            ),
            "audit": [{
                "check": "record_candidate_uniqueness",
                "status": "fail",
                "details": [
                    {
                        "evidence_id": item["record"]["evidence_id"],
                        "subject": item["subject"]["value"],
                        "status": (item["status"] or {}).get("value"),
                    }
                    for item in candidates[:8]
                ],
            }],
        })
    candidate = candidates[0]
    row_record = candidate["record"]
    row_id = row_record["evidence_id"]
    unplanned_fields = _unplanned_record_field_mentions(
        question, question_plan, candidate, plan_items
    )
    if unplanned_fields:
        return _finish({
            **base,
            "status": "hold",
            "reason": "record_lookup_question_field_not_planned",
            "audit": [{
                "check": "question_plan_field_coverage",
                "status": "fail",
                "details": unplanned_fields,
            }],
        })

    subject_lineage, subject_error = _record_field_lineage(
        stored_traversal,
        evidence_universe,
        candidate,
        candidate["subject"]["label"],
        candidate["subject"]["value"],
        None,
    )
    if subject_error is not None:
        return _finish({
            **base,
            "status": "hold",
            "reason": "stored_graph_lineage_failed",
            "audit": [{
                "check": "stored_graph_record_subject_lineage",
                "status": "fail",
                "details": subject_error,
            }],
        })

    temporal_lineage = None
    if temporal_context is not None:
        temporal_candidate = candidate["temporal"]
        start_lineage, start_error = _record_field_lineage(
            stored_traversal,
            evidence_universe,
            candidate,
            temporal_candidate["start"]["label"],
            temporal_candidate["start"]["value"],
            None,
        )
        end_lineage, end_error = _record_field_lineage(
            stored_traversal,
            evidence_universe,
            candidate,
            temporal_candidate["end"]["label"],
            temporal_candidate["end"]["value"],
            None,
        )
        if start_error is not None or end_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_lineage_failed",
                "audit": [{
                    "check": "stored_graph_assignment_period_lineage",
                    "status": "fail",
                    "details": {"start": start_error, "end": end_error},
                }],
            })
        temporal_lineage = {
            "scope": dict(temporal_context["scope"]),
            "target": temporal_context["target"],
            "relation": temporal_context["relation"],
            "start": start_lineage,
            "end": end_lineage,
            "source_evidence_ids": list(dict.fromkeys([
                *start_lineage["source_evidence_ids"],
                *end_lineage["source_evidence_ids"],
            ])),
            "relation_ids": list(dict.fromkeys([
                *start_lineage["relation_ids"],
                *end_lineage["relation_ids"],
            ])),
        }
    temporal_exclusion_validation_ids = list(dict.fromkeys(
        evidence_id
        for lineage in temporal_exclusion_lineages
        for evidence_id in (
            lineage["record_evidence_id"],
            *lineage["source_evidence_ids"],
        )
    ))
    temporal_exclusion_relation_ids = list(dict.fromkeys(
        relation_id
        for lineage in temporal_exclusion_lineages
        for relation_id in lineage["relation_ids"]
    ))

    status_lineage = None
    if require_final:
        status_lineage, status_error = _record_field_lineage(
            stored_traversal,
            evidence_universe,
            candidate,
            candidate["status"]["label"],
            candidate["status"]["value"],
            None,
        )
        if status_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_lineage_failed",
                "audit": [{
                    "check": "stored_graph_record_status_lineage",
                    "status": "fail",
                    "details": status_error,
                }],
            })

    question_node = "node_question"
    operation_node = "node_operation_record_lookup"
    record_node = "node_record_" + stable_hash(row_id)[:16]
    nodes: list[dict[str, Any]] = [
        {
            "node_id": question_node,
            "node_type": "question",
            "value_sha256": base["query_sha256"],
        },
        {
            "node_id": operation_node,
            "node_type": "operation",
            "value": "record_lookup",
        },
        {
            "node_id": record_node,
            "node_type": "record",
            "value": candidate["subject"]["value"],
            "record_evidence_id": row_id,
        },
    ]
    time_node = None
    assignment_period_node = None
    temporal_exclusion_nodes: list[tuple[str, dict[str, Any]]] = []
    if temporal_context is not None:
        time_node = "node_as_of_" + stable_hash(temporal_context["scope"])[:16]
        assignment_period_node = "node_assignment_period_" + stable_hash({
            "record_evidence_id": row_id,
            "start_date": candidate["temporal"]["start_date"],
            "end_date": candidate["temporal"]["end_date"],
        })[:16]
        nodes.extend([
            {
                "node_id": time_node,
                "node_type": "time_point",
                "value": temporal_context["scope"]["as_of"],
                "expression": temporal_context["scope"]["expression"],
                "reference_date": temporal_context["scope"]["reference_date"],
                "timezone": temporal_context["scope"]["timezone"],
                "precision": temporal_context["scope"]["precision"],
            },
            {
                "node_id": assignment_period_node,
                "node_type": "assignment_period",
                "target": temporal_context["target"],
                "relation": temporal_context["relation"],
                "start_date": candidate["temporal"]["start_date"],
                "end_date": candidate["temporal"]["end_date"],
                "boundary": temporal_context["scope"]["boundary"],
                "record_evidence_id": row_id,
            },
        ])
        for exclusion in temporal_exclusion_lineages:
            exclusion_node = "node_excluded_assignment_period_" + stable_hash({
                "record_evidence_id": exclusion["record_evidence_id"],
                "start_date": exclusion["start_date"],
                "end_date": exclusion["end_date"],
                "reason": exclusion["reason"],
            })[:16]
            nodes.append({
                "node_id": exclusion_node,
                "node_type": "assignment_period",
                "target": temporal_context["target"],
                "relation": temporal_context["relation"],
                "start_date": exclusion["start_date"],
                "end_date": exclusion["end_date"],
                "boundary": temporal_context["scope"]["boundary"],
                "record_evidence_id": exclusion["record_evidence_id"],
                "selection_status": "excluded",
                "exclusion_reason": exclusion["reason"],
            })
            temporal_exclusion_nodes.append((exclusion_node, exclusion))
    edges: list[dict[str, Any]] = [{
        "edge_id": "edge_question_requires_record_lookup",
        "source": question_node,
        "predicate": "requires",
        "target": operation_node,
        "basis": {
            "kind": "explicit",
            "rule": "valid_question_plan_items",
            "evidence_ids": [],
        },
    }]
    if temporal_context is None:
        edges.append({
            "edge_id": "edge_lookup_selects_record",
            "source": operation_node,
            "predicate": "selects",
            "target": record_node,
            "basis": {
                "kind": "explicit",
                "rule": "question_contains_unique_structured_row_subject",
                "evidence_ids": list(dict.fromkeys([
                    row_id,
                    *subject_lineage["source_evidence_ids"],
                    *(
                        status_lineage["source_evidence_ids"]
                        if status_lineage is not None else []
                    ),
                ])),
            },
        })
    else:
        temporal_evidence_ids = list(dict.fromkeys([
            row_id,
            *subject_lineage["source_evidence_ids"],
            *temporal_lineage["source_evidence_ids"],
        ]))
        edges.extend([
            {
                "edge_id": "edge_lookup_evaluates_at",
                "source": operation_node,
                "predicate": "evaluates_at",
                "target": time_node,
                "basis": {
                    "kind": "inference",
                    "rule": TEMPORAL_RESOLUTION_RULE,
                    "evidence_ids": [],
                    "question_plan_sha256": base["intent"]["question_plan_sha256"],
                },
            },
            {
                "edge_id": "edge_time_within_assignment_period",
                "source": time_node,
                "predicate": "falls_within",
                "target": assignment_period_node,
                "basis": {
                    "kind": "inference",
                    "rule": "inclusive_validated_assignment_period",
                    "evidence_ids": temporal_evidence_ids,
                },
            },
            {
                "edge_id": "edge_assignment_period_selects_record",
                "source": assignment_period_node,
                "predicate": "selects_assignment",
                "target": record_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "same_row_subject_start_end_lineage",
                    "evidence_ids": temporal_evidence_ids,
                },
            },
        ])
        for exclusion_node, exclusion in temporal_exclusion_nodes:
            edges.append({
                "edge_id": "edge_time_outside_assignment_period_" + stable_hash({
                    "node": exclusion_node,
                    "as_of": temporal_context["scope"]["as_of"],
                })[:16],
                "source": time_node,
                "predicate": "falls_outside",
                "target": exclusion_node,
                "basis": {
                    "kind": "inference",
                    "rule": exclusion["reason"],
                    "evidence_ids": list(dict.fromkeys([
                        exclusion["record_evidence_id"],
                        *exclusion["source_evidence_ids"],
                    ])),
                },
            })
    branches = []
    top_selected_ids: list[str] = []
    top_validation_ids: list[str] = []
    branch_lineages = []
    for item in plan_items:
        row_field = candidate["requested"][item["item_id"]]
        field_lineage, lineage_error = _record_field_lineage(
            stored_traversal,
            evidence_universe,
            candidate,
            row_field["label"],
            row_field["value"],
            item["field_name"],
        )
        if lineage_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_lineage_failed",
                "audit": [{
                    "check": "stored_graph_record_field_lineage",
                    "status": "fail",
                    "details": {
                        "item_id": item["item_id"],
                        **lineage_error,
                    },
                }],
            })
        branch_id = "branch_" + stable_hash({
            "item_id": item["item_id"],
            "field_name": item["field_name"],
        })[:16]
        field_node = "node_field_" + stable_hash({
            "branch_id": branch_id,
            "label": row_field["label"],
        })[:16]
        value_node = "node_value_" + stable_hash({
            "branch_id": branch_id,
            "value": field_lineage["value"],
        })[:16]
        nodes.extend([
            {
                "node_id": field_node,
                "node_type": "field",
                "field_name": item["field_name"],
                "value": row_field["label"],
            },
            {
                "node_id": value_node,
                "node_type": "value",
                "value": field_lineage["value"],
            },
        ])
        selected_ids = list(dict.fromkeys([
            row_id,
            field_lineage["header_evidence_id"],
            field_lineage["value_evidence_id"],
        ]))
        validation_ids = list(dict.fromkeys([
            *selected_ids,
            *subject_lineage["source_evidence_ids"],
            *temporal_exclusion_validation_ids,
            *(
                temporal_lineage["source_evidence_ids"]
                if temporal_lineage is not None else []
            ),
            *(
                status_lineage["source_evidence_ids"]
                if status_lineage is not None else []
            ),
        ]))
        relation_ids = list(dict.fromkeys([
            *subject_lineage["relation_ids"],
            *field_lineage["relation_ids"],
            *temporal_exclusion_relation_ids,
            *(
                temporal_lineage["relation_ids"]
                if temporal_lineage is not None else []
            ),
            *(
                status_lineage["relation_ids"]
                if status_lineage is not None else []
            ),
        ]))
        branch_lineage = {
            "branch_id": branch_id,
            "item_id": item["item_id"],
            "record_search_unit_id": row_id,
            "subject": subject_lineage,
            "field": field_lineage,
            "status": status_lineage,
            "temporal": temporal_lineage,
            "temporal_exclusions": temporal_exclusion_lineages,
            "source_evidence_ids": list(dict.fromkeys([
                *subject_lineage["source_evidence_ids"],
                *field_lineage["source_evidence_ids"],
                *temporal_exclusion_validation_ids,
                *(
                    temporal_lineage["source_evidence_ids"]
                    if temporal_lineage is not None else []
                ),
                *(
                    status_lineage["source_evidence_ids"]
                    if status_lineage is not None else []
                ),
            ])),
            "relation_ids": relation_ids,
        }
        branch_binding = _stored_graph_binding(
            stored_traversal,
            validation_ids,
            record_lookup_lineage=branch_lineage,
        )
        primary_path = [question_node, operation_node]
        if temporal_context is not None:
            primary_path.extend([time_node, assignment_period_node])
        primary_path.extend([record_node, field_node, value_node])
        branches.append({
            "branch_id": branch_id,
            "item_id": item["item_id"],
            "label": item["label"],
            "field_name": item["field_name"],
            "source_label": row_field["label"],
            "source_value": row_field["value"],
            "value": field_lineage["value"],
            "selected_evidence_ids": selected_ids,
            "validation_evidence_ids": validation_ids,
            "primary_path": primary_path,
            "stored_graph_binding": branch_binding,
        })
        branch_lineages.append(branch_lineage)
        top_selected_ids.extend(selected_ids)
        top_validation_ids.extend(validation_ids)
        edge_suffix = stable_hash(branch_id)[:16]
        edges.extend([
            {
                "edge_id": f"edge_record_has_field_{edge_suffix}",
                "source": record_node,
                "predicate": "has_field",
                "target": field_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "verified_row_to_header_lineage",
                    "evidence_ids": [row_id, field_lineage["header_evidence_id"]],
                },
            },
            {
                "edge_id": f"edge_field_resolves_value_{edge_suffix}",
                "source": field_node,
                "predicate": "resolves_as",
                "target": value_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "verified_row_to_value_cell_lineage",
                    "evidence_ids": [row_id, field_lineage["value_evidence_id"]],
                },
            },
            {
                "edge_id": f"edge_value_answers_item_{edge_suffix}",
                "source": value_node,
                "predicate": "answers_item",
                "target": question_node,
                "basis": {
                    "kind": "inference",
                    "rule": "unique_record_field_projection",
                    "evidence_ids": selected_ids,
                    "item_id": item["item_id"],
                },
            },
        ])
        if temporal_context is not None and item["field_name"] == "owner":
            edges.append({
                "edge_id": f"edge_owner_responsible_for_record_{edge_suffix}",
                "source": value_node,
                "predicate": "responsible_for",
                "target": record_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "same_row_owner_subject_and_validated_period",
                    "evidence_ids": list(dict.fromkeys([
                        row_id,
                        *subject_lineage["source_evidence_ids"],
                        *field_lineage["source_evidence_ids"],
                        *temporal_lineage["source_evidence_ids"],
                    ])),
                },
            })

    top_selected_ids = list(dict.fromkeys(top_selected_ids))
    top_validation_ids = list(dict.fromkeys(top_validation_ids))
    record_lookup_lineage = {
        "record_search_unit_id": row_id,
        "subject": subject_lineage,
        "status": status_lineage,
        "temporal": temporal_lineage,
        "temporal_exclusions": temporal_exclusion_lineages,
        "branches": branch_lineages,
        "source_evidence_ids": list(dict.fromkeys(
            evidence_id
            for branch_lineage in branch_lineages
            for evidence_id in branch_lineage["source_evidence_ids"]
        )),
        "relation_ids": sorted({
            relation_id
            for branch_lineage in branch_lineages
            for relation_id in branch_lineage["relation_ids"]
        }),
    }
    stored_binding = _stored_graph_binding(
        stored_traversal,
        top_validation_ids,
        record_lookup_lineage=record_lookup_lineage,
    )
    record_by_id = {
        record["evidence_id"]: record for record in evidence_universe
    }
    provisional = [
        evidence_id for evidence_id in top_validation_ids
        if PROVISIONAL_MARKER in record_by_id[evidence_id]["text"]
    ]
    if provisional:
        return _finish({
            **base,
            "status": "hold",
            "reason": "provisional_record_lookup_evidence",
            "audit": [{
                "check": "confirmed_evidence_only",
                "status": "fail",
                "details": provisional,
            }],
        })
    for evidence_id in top_validation_ids:
        record = record_by_id[evidence_id]
        nodes.append({
            "node_id": evidence_id,
            "node_type": "evidence",
            "evidence_id": evidence_id,
            "document_id": record["document_id"],
            "relative_path": record["relative_path"],
            "locator": record["locator"],
            "observed_sha256": record["observed_sha256"],
        })

    return _finish({
        **base,
        "status": "ready",
        "reason": "record_lookup_graph_verified",
        "nodes": nodes,
        "edges": edges,
        "primary_path": branches[0]["primary_path"],
        "branches": branches,
        "selected_evidence_ids": top_selected_ids,
        "selection": {
            "method": (
                "unique_as_of_assignment_and_verified_cell_lineage"
                if temporal_context is not None
                else "unique_subject_row_and_verified_cell_lineage"
            ),
            "record_evidence_id": row_id,
            "subject_label": candidate["subject"]["label"],
            "subject_value": candidate["subject"]["value"],
            "status": (candidate["status"] or {}).get("value"),
            "document_id": row_record["document_id"],
            "relative_path": row_record["relative_path"],
            "sheet_name": candidate["sheet_name"],
            "row_index": candidate["row_index"],
            "temporal_scope": (
                dict(temporal_context["scope"])
                if temporal_context is not None else None
            ),
            "assignment_period": (
                {
                    "start_date": candidate["temporal"]["start_date"],
                    "end_date": candidate["temporal"]["end_date"],
                    "boundary": temporal_context["scope"]["boundary"],
                    "start_evidence_id": temporal_lineage["start"]["value_evidence_id"],
                    "end_evidence_id": temporal_lineage["end"]["value_evidence_id"],
                }
                if temporal_context is not None else None
            ),
            "excluded_assignments": [
                {
                    "record_evidence_id": lineage["record_evidence_id"],
                    "start_date": lineage["start_date"],
                    "end_date": lineage["end_date"],
                    "reason": lineage["reason"],
                    "validation_evidence_ids": list(dict.fromkeys([
                        lineage["record_evidence_id"],
                        *lineage["source_evidence_ids"],
                    ])),
                }
                for lineage in temporal_exclusion_lineages
            ],
            "values": {
                branch["item_id"]: branch["value"] for branch in branches
            },
            "selected_evidence_ids": top_selected_ids,
            "validation_evidence_ids": top_validation_ids,
        },
        "stored_graph_binding": stored_binding,
        "audit": [
            {
                "check": "record_candidate_uniqueness",
                "status": "pass",
                "details": row_id,
            },
            {
                "check": "record_status",
                "status": "pass",
                "details": (candidate["status"] or {}).get("value"),
            },
            {
                "check": "requested_field_lineage",
                "status": "pass",
                "details": [branch["item_id"] for branch in branches],
            },
            *([{
                "check": "assignment_period_contains_as_of",
                "status": "pass",
                "details": {
                    "as_of": temporal_context["scope"]["as_of"],
                    "start_date": candidate["temporal"]["start_date"],
                    "end_date": candidate["temporal"]["end_date"],
                    "boundary": temporal_context["scope"]["boundary"],
                },
            }] if temporal_context is not None else []),
            *([{
                "check": "excluded_assignment_period_lineage",
                "status": "pass",
                "details": [
                    lineage["record_evidence_id"]
                    for lineage in temporal_exclusion_lineages
                ],
            }] if temporal_context is not None else []),
        ],
    })


def build_question_evidence_graph(
    question: str,
    evidence_records: Iterable[Mapping[str, Any]],
    source_graph: Mapping[str, Any] | None = None,
    *,
    question_plan: Mapping[str, Any] | None = None,
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Compile an immutable question overlay over Evidence and its source Graph."""
    records = []
    seen_ids = set()
    invalid_ids = []
    malformed_records = []
    integrity_errors = []
    for raw in evidence_records:
        record = _evidence_record(raw)
        if record is None:
            malformed_records.append({
                "evidence_id": str(raw.get("evidence_id", ""))
                if isinstance(raw, Mapping) else "",
                "record_type": type(raw).__name__,
            })
            continue
        if record["evidence_id"] in seen_ids:
            invalid_ids.append(record["evidence_id"])
            continue
        seen_ids.add(record["evidence_id"])
        records.append(record)
        if record["integrity_error"]:
            integrity_errors.append({
                "evidence_id": record["evidence_id"],
                "code": record["integrity_error"],
            })
    records.sort(key=lambda item: (
        item["document_id"], item["relative_path"], canonical_json(item["locator"]),
        item["evidence_id"],
    ))
    malformed_records.sort(key=lambda item: (item["evidence_id"], item["record_type"]))

    count_applicable, surfaces = _count_intent(question)
    strong_count = any(surface in STRONG_COUNT_SURFACES for surface in surfaces)
    record_plan = (
        None if strong_count else _record_lookup_plan(
            question_plan,
            activate_unknown=bool(_mentioned_record_fields(question)),
            question=question,
        )
    )
    applicable = count_applicable and record_plan is None
    operation = (
        "aggregate_count" if applicable
        else "record_lookup" if record_plan is not None
        else "unknown"
    )
    temporal_context = None
    temporal_error = None
    if record_plan is not None and isinstance(question_plan, Mapping):
        temporal_context, temporal_error = _validated_temporal_context(
            question, question_plan, reference_date
        )
    scope = _question_scope(question)
    intent = {
        "operation": operation,
        "answer_shape": (
            {"container": "scalar", "value_type": "integer", "unit": "回"}
            if applicable
            else {"container": "record", "value_type": "field_map", "unit": None}
            if record_plan is not None
            else {"container": "unknown", "value_type": "unknown", "unit": None}
        ),
        "explicit_surfaces": surfaces,
        "time_scope": scope,
    }
    if record_plan is not None:
        intent["question_plan_sha256"] = _record_lookup_plan_hash(question_plan)
        intent["requested_items"] = record_plan
    if temporal_context is not None:
        intent.update({
            "temporal_scope": dict(temporal_context["scope"]),
            "temporal_target": temporal_context["target"],
            "temporal_relation": temporal_context["relation"],
            "reference_context_sha256": stable_hash({
                "reference_date": temporal_context["scope"]["reference_date"],
                "timezone": temporal_context["scope"]["timezone"],
                "resolution_rule": temporal_context["scope"]["resolution_rule"],
            }),
        })
    base = {
        "artifact_version": GRAPH_VERSION,
        "record_type": "question_evidence_graph",
        "query_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "status": "unsupported",
        "reason": "question_operation_not_supported",
        "intent": intent,
        "nodes": [],
        "edges": [],
        "primary_path": [],
        "selected_evidence_ids": [],
        "selection": None,
        "stored_graph_binding": None,
        "audit": [],
    }
    if record_plan is not None:
        base["branches"] = []
    if not applicable and record_plan is None:
        return _finish(base)
    if record_plan is not None:
        if temporal_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_temporal_scope_invalid",
                "audit": [{
                    "check": "temporal_scope_contract",
                    "status": "fail",
                    "details": temporal_error,
                }],
            })
        invalid_items = [
            {
                "item_id": item["item_id"],
                "errors": item["plan_errors"],
            }
            for item in record_plan if item["plan_errors"]
        ]
        if invalid_items:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_plan_invalid",
                "audit": [{
                    "check": "question_plan_shape",
                    "status": "fail",
                    "details": invalid_items,
                }],
            })
        ambiguous_items = [
            item["item_id"] for item in record_plan
            if item["field_name"] is None
            and len(item["mentioned_field_names"]) > 1
        ]
        if ambiguous_items:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_field_ambiguous",
                "audit": [{
                    "check": "question_plan_field_cardinality",
                    "status": "fail",
                    "details": ambiguous_items,
                }],
            })
        unsupported_items = [
            item["item_id"] for item in record_plan
            if item["field_name"] is None
        ]
        if unsupported_items:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_field_not_supported",
                "audit": [{
                    "check": "question_plan_field_support",
                    "status": "fail",
                    "details": unsupported_items,
                }],
            })
        item_ids_by_field: dict[str, list[str]] = {}
        for item in record_plan:
            item_ids_by_field.setdefault(item["field_name"], []).append(
                item["item_id"]
            )
        duplicate_fields = {
            field_name: item_ids
            for field_name, item_ids in item_ids_by_field.items()
            if len(item_ids) > 1
        }
        if duplicate_fields:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_field_duplicate",
                "audit": [{
                    "check": "question_plan_field_uniqueness",
                    "status": "fail",
                    "details": duplicate_fields,
                }],
            })
        ungrounded_items = _ungrounded_record_plan_items(question, record_plan)
        if ungrounded_items:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_plan_not_grounded",
                "audit": [{
                    "check": "question_plan_grounding",
                    "status": "fail",
                    "details": ungrounded_items,
                }],
            })
        normalized_question = _record_status_scope_text(
            question, record_plan, temporal_context, records
        )
        if NEGATED_FINAL_RECORD_SURFACE.search(normalized_question):
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_negated_final_not_supported",
                "audit": [{
                    "check": "record_finality_scope",
                    "status": "fail",
                    "details": "negated final-status surface",
                }],
            })
        has_final_scope = bool(FINAL_RECORD_SURFACE.search(normalized_question))
        has_nonfinal_scope = bool(
            NONFINAL_RECORD_SURFACE.search(normalized_question)
        )
        if has_final_scope and has_nonfinal_scope:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_status_scope_conflicting",
                "audit": [{
                    "check": "record_status_scope",
                    "status": "fail",
                    "details": "final and non-final status surfaces",
                }],
            })
        if has_nonfinal_scope:
            return _finish({
                **base,
                "status": "hold",
                "reason": "record_lookup_status_scope_not_supported",
                "audit": [{
                    "check": "record_status_scope",
                    "status": "fail",
                    "details": "non-final status surface",
                }],
            })
    if malformed_records:
        return _finish({
            **base, "status": "hold", "reason": "malformed_evidence_records",
            "audit": [{
                "check": "evidence_record_shape", "status": "fail",
                "details": malformed_records[:8],
            }],
        })
    if invalid_ids:
        return _finish({
            **base, "status": "hold", "reason": "duplicate_evidence_ids",
            "audit": [{"check": "evidence_ids_unique", "status": "fail", "details": invalid_ids[:8]}],
        })
    if integrity_errors:
        return _finish({
            **base, "status": "hold", "reason": "evidence_text_hash_mismatch",
            "audit": [{
                "check": "evidence_text_integrity", "status": "fail",
                "details": integrity_errors[:8],
            }],
        })

    stored_traversal = None
    evidence_universe = records
    if source_graph is not None:
        stored_traversal, traversal_error = _prepare_stored_graph_traversal(
            source_graph, records
        )
        if traversal_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_traversal_failed",
                "audit": [{
                    "check": "stored_graph_traversal",
                    "status": "fail",
                    "details": traversal_error,
                }],
            })
        if temporal_context is not None:
            unreachable_target_rows = _unreachable_temporal_target_rows(
                records,
                stored_traversal,
                temporal_context["target"],
            )
            if unreachable_target_rows:
                return _finish({
                    **base,
                    "status": "hold",
                    "reason": "stored_graph_traversal_failed",
                    "audit": [{
                        "check": "stored_graph_temporal_target_reachability",
                        "status": "fail",
                        "details": {
                            "code": "temporal_target_row_unreachable",
                            "rows": unreachable_target_rows[:8],
                        },
                    }],
                })
        records = [
            record for record in records
            if record["evidence_id"] in stored_traversal["paths"]
        ]
        if not records:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_traversal_failed",
                "audit": [{
                    "check": "stored_graph_traversal",
                    "status": "fail",
                    "details": {
                        "code": "source_graph_reachable_evidence_missing",
                        "detail": stored_traversal["unreachable_evidence_ids"][:8],
                    },
                }],
            })

    if record_plan is not None:
        return _build_record_lookup_graph(
            base,
            question,
            records,
            evidence_universe,
            stored_traversal,
            record_plan,
            question_plan,
            temporal_context,
        )

    candidates = _candidate_rows(records, question, scope)
    if not candidates:
        return _finish({
            **base, "status": "hold", "reason": "structured_aggregate_not_found",
            "audit": [{"check": "structured_aggregate", "status": "fail", "details": []}],
        })
    best_score = max(candidate["score"] for candidate in candidates)
    best = [candidate for candidate in candidates if candidate["score"] == best_score]
    if best_score < 0.35:
        return _finish({
            **base, "status": "hold", "reason": "aggregate_subject_unresolved",
            "audit": [{"check": "target_binding", "status": "fail", "details": []}],
        })
    distinct = {
        (
            candidate["record"]["document_id"], candidate["record"]["relative_path"],
            candidate["record"]["evidence_id"], candidate["sheet_name"],
            candidate["row_index"], candidate["label"], candidate["formula"],
            _decimal_text(candidate["saved_value"]),
        )
        for candidate in best
    }
    if len(distinct) != 1:
        return _finish({
            **base, "status": "hold", "reason": "aggregate_candidate_ambiguous",
            "audit": [{"check": "candidate_uniqueness", "status": "fail", "details": sorted(map(str, distinct))[:8]}],
        })
    candidate = sorted(best, key=lambda item: (
        item["record"]["document_id"], item["record"]["relative_path"],
        item["sheet_name"], item["row_index"], item["record"]["evidence_id"],
    ))[0]
    selected_signature = next(iter(distinct))
    competing_signatures = {
        (
            other["record"]["document_id"],
            other["record"]["relative_path"],
            other["record"]["evidence_id"],
            other["sheet_name"],
            other["row_index"],
            other["label"],
            other["formula"],
            _decimal_text(other["saved_value"]),
        )
        for other in candidates
        if other["score"] >= max(0.35, best_score * 0.60)
    } - {selected_signature}
    if competing_signatures:
        return _finish({
            **base,
            "status": "hold",
            "reason": "aggregate_candidate_competing",
            "audit": [{
                "check": "candidate_competition",
                "status": "fail",
                "details": sorted(map(str, competing_signatures))[:8],
            }],
        })
    companions = _cell_companions(records, candidate)
    structured_lineage = None
    projection_records = records
    if stored_traversal is not None:
        structured_lineage, projection_records, lineage_error = (
            _structured_aggregate_lineage(
                stored_traversal, records, evidence_universe, candidate, companions
            )
        )
        if lineage_error is not None:
            return _finish({
                **base,
                "status": "hold",
                "reason": "stored_graph_lineage_failed",
                "audit": [{
                    "check": "stored_graph_structured_lineage",
                    "status": "fail",
                    "details": lineage_error,
                }],
            })
    projection = _row_projection(projection_records, candidate)
    if not projection["complete"]:
        return _finish({
            **base, "status": "hold", "reason": "aggregation_coverage_incomplete",
            "audit": [{
                "check": "range_coverage", "status": "fail",
                "details": {"missing_rows": projection["missing_rows"], "conflicting_rows": projection["conflicting_rows"]},
            }],
        })
    recomputed = _decimal(projection["recomputed_value"])
    if recomputed != candidate["saved_value"]:
        return _finish({
            **base, "status": "hold", "reason": "aggregate_value_conflict",
            "audit": [{
                "check": "saved_value_matches_recomputation", "status": "fail",
                "details": {
                    "saved_value": _decimal_text(candidate["saved_value"]),
                    "recomputed_value": projection["recomputed_value"],
                },
            }],
        })

    references = _reference_records(records, candidate)
    aggregate_ids = [candidate["record"]["evidence_id"]] + [
        record["evidence_id"] for record in companions
    ]
    aggregate_ids = list(dict.fromkeys(aggregate_ids))
    selected_ids = list(dict.fromkeys(
        aggregate_ids + [record["evidence_id"] for record in references]
        + projection["nonzero_evidence_ids"][:2]
    ))
    validation_ids = list(dict.fromkeys(
        aggregate_ids + projection["validation_evidence_ids"]
        + [record["evidence_id"] for record in references]
    ))
    stored_binding = None
    if stored_traversal is not None:
        binding_ids = list(dict.fromkeys(
            validation_ids + structured_lineage["source_evidence_ids"]
        ))
        stored_binding = _stored_graph_binding(
            stored_traversal, binding_ids, structured_lineage
        )
    record_by_id = {record["evidence_id"]: record for record in records}
    provisional = [
        evidence_id for evidence_id in validation_ids
        if PROVISIONAL_MARKER in record_by_id[evidence_id]["text"]
    ]
    if provisional:
        return _finish({
            **base, "status": "hold", "reason": "provisional_aggregate_evidence",
            "audit": [{"check": "confirmed_evidence_only", "status": "fail", "details": provisional}],
        })

    question_node = "node_question"
    operation_node = "node_operation_aggregate_count"
    target_node = "node_target_" + stable_hash(candidate["label"])[:16]
    value_text = _decimal_text(candidate["saved_value"])
    value_node = "node_value_" + stable_hash(value_text)[:16]
    nodes = [
        {"node_id": question_node, "node_type": "question", "value_sha256": base["query_sha256"]},
        {"node_id": operation_node, "node_type": "operation", "value": "aggregate_count"},
        {"node_id": target_node, "node_type": "target", "value": candidate["label"]},
        {"node_id": value_node, "node_type": "value", "value": value_text, "unit": "回"},
    ]
    for evidence_id in validation_ids:
        record = record_by_id[evidence_id]
        nodes.append({
            "node_id": evidence_id,
            "node_type": "evidence",
            "evidence_id": evidence_id,
            "document_id": record["document_id"],
            "relative_path": record["relative_path"],
            "locator": record["locator"],
            "observed_sha256": record["observed_sha256"],
        })
    common_edges = [
        {
            "edge_id": "edge_question_requires_count",
            "source": question_node, "predicate": "requires", "target": operation_node,
            "basis": {"kind": "explicit", "rule": "count_surface", "evidence_ids": []},
        },
        {
            "edge_id": "edge_count_targets_field",
            "source": operation_node, "predicate": "targets", "target": target_node,
            "basis": {
                "kind": "inference", "rule": "unique_subject_label_overlap",
                "evidence_ids": [candidate["record"]["evidence_id"]],
            },
        },
    ]
    if stored_binding is None:
        edges = common_edges + [
            {
                "edge_id": "edge_formula_aggregates_rows",
                "source": candidate["record"]["evidence_id"], "predicate": "aggregates", "target": target_node,
                "basis": {
                    "kind": "explicit", "rule": "sum_formula_range",
                    "evidence_ids": aggregate_ids,
                },
            },
            {
                "edge_id": "edge_rows_support_value",
                "source": target_node, "predicate": "recomputed_as", "target": value_node,
                "basis": {
                    "kind": "inference", "rule": "complete_structured_range_sum",
                    "evidence_ids": projection["validation_evidence_ids"],
                },
            },
            {
                "edge_id": "edge_value_answers_question",
                "source": value_node, "predicate": "answers", "target": question_node,
                "basis": {
                    "kind": "inference", "rule": "saved_and_recomputed_values_agree",
                    "evidence_ids": aggregate_ids + projection["nonzero_evidence_ids"],
                },
            },
        ]
        primary_path = [question_node, operation_node, target_node, value_node]
    else:
        range_text = (
            f"{candidate['column']}{candidate['start_row']}:"
            f"{candidate['column']}{candidate['end_row']}"
        )
        range_node = "node_range_" + stable_hash({
            "document_id": candidate["record"]["document_id"],
            "sheet_name": candidate["sheet_name"],
            "range": range_text,
        })[:16]
        saved_value_node = "node_saved_value_" + stable_hash(value_text)[:16]
        nodes.extend([
            {
                "node_id": range_node,
                "node_type": "range",
                "sheet_name": candidate["sheet_name"],
                "value": range_text,
            },
            {
                "node_id": saved_value_node,
                "node_type": "saved_value",
                "value": value_text,
                "unit": "回",
            },
        ])
        formula_ids = [
            record["evidence_id"] for record in companions
            if SUM_FORMULA.search(unicodedata.normalize("NFKC", record["text"]))
        ]
        saved_ids = [
            record["evidence_id"] for record in companions
            if _decimal(record["text"]) == candidate["saved_value"]
        ]
        edges = common_edges + [
            {
                "edge_id": "edge_target_uses_range",
                "source": target_node,
                "predicate": "uses_range",
                "target": range_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "sum_formula_range",
                    "evidence_ids": [candidate["record"]["evidence_id"], *formula_ids],
                },
            },
        ]
        for evidence_id in projection["validation_evidence_ids"]:
            row_index = record_by_id[evidence_id]["locator"].get("row_index")
            edges.extend([
                {
                    "edge_id": f"edge_range_includes_{evidence_id}",
                    "source": range_node,
                    "predicate": "includes",
                    "target": evidence_id,
                    "basis": {
                        "kind": "explicit",
                        "rule": "sum_formula_row_membership",
                        "evidence_ids": [candidate["record"]["evidence_id"], evidence_id],
                    },
                },
                {
                    "edge_id": f"edge_row_contributes_{evidence_id}",
                    "source": evidence_id,
                    "predicate": "contributes_to",
                    "target": value_node,
                    "basis": {
                        "kind": "inference",
                        "rule": "structured_row_numeric_projection",
                        "evidence_ids": [evidence_id],
                        "row_index": row_index,
                    },
                },
            ])
        for evidence_id in saved_ids:
            edges.append({
                "edge_id": f"edge_saved_value_{evidence_id}",
                "source": evidence_id,
                "predicate": "asserts",
                "target": saved_value_node,
                "basis": {
                    "kind": "explicit",
                    "rule": "stored_workbook_value",
                    "evidence_ids": [evidence_id],
                },
            })
        edges.extend([
            {
                "edge_id": "edge_range_recomputed_as_value",
                "source": range_node,
                "predicate": "recomputed_as",
                "target": value_node,
                "basis": {
                    "kind": "inference",
                    "rule": "complete_structured_range_sum",
                    "evidence_ids": projection["validation_evidence_ids"],
                },
            },
            {
                "edge_id": "edge_saved_agrees_with_recomputed",
                "source": saved_value_node,
                "predicate": "agrees_with",
                "target": value_node,
                "basis": {
                    "kind": "inference",
                    "rule": "saved_and_recomputed_values_agree",
                    "evidence_ids": aggregate_ids + projection["validation_evidence_ids"],
                },
            },
            {
                "edge_id": "edge_value_answers_question",
                "source": value_node,
                "predicate": "answers",
                "target": question_node,
                "basis": {
                    "kind": "inference",
                    "rule": "verified_graph_aggregate_answers_count_question",
                    "evidence_ids": aggregate_ids + projection["validation_evidence_ids"],
                },
            },
        ])
        primary_path = [
            question_node, operation_node, target_node, range_node, value_node,
        ]
    body = {
        **base,
        "status": "ready",
        "reason": "aggregate_graph_verified",
        "nodes": nodes,
        "edges": edges,
        "primary_path": primary_path,
        "selected_evidence_ids": selected_ids,
        "selection": {
            "candidate_id": candidate["candidate_id"],
            "value": value_text,
            "saved_value": value_text,
            "recomputed_value": projection["recomputed_value"],
            "unit": "回",
            "method": "explicit_sum_and_complete_structured_recomputation",
            "target_label": candidate["label"],
            "document_id": candidate["record"]["document_id"],
            "relative_path": candidate["record"]["relative_path"],
            "sheet_name": candidate["sheet_name"],
            "formula": candidate["formula"],
            "range": f"{candidate['column']}{candidate['start_row']}:{candidate['column']}{candidate['end_row']}",
            "mandatory_aggregation_evidence_ids": aggregate_ids,
            "validation_evidence_ids": validation_ids,
            "selected_evidence_ids": selected_ids,
            "coverage": {
                "expected_rows": projection["expected_row_count"],
                "covered_rows": projection["covered_row_count"],
            },
            "matching_reference_evidence_ids": [record["evidence_id"] for record in references],
        },
        "stored_graph_binding": stored_binding,
        "audit": [
            {"check": "target_binding", "status": "pass", "details": candidate["label"]},
            {
                "check": "range_coverage", "status": "pass",
                "details": f"{projection['covered_row_count']}/{projection['expected_row_count']}",
            },
            {
                "check": "saved_value_matches_recomputation", "status": "pass",
                "details": value_text,
            },
        ],
    }
    return _finish(body)


def validate_question_evidence_graph(
    question: str,
    evidence_records: Iterable[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    source_graph: Mapping[str, Any] | None = None,
    *,
    question_plan: Mapping[str, Any] | None = None,
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Independently rebuild and compare a Question Evidence Graph Artifact."""
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not isinstance(artifact, Mapping):
        return {
            "validator_version": VALIDATOR_VERSION, "status": "blocked",
            "failures": [{"code": "artifact_missing", "detail": "Question Evidence Graph is missing."}],
            "warnings": [], "checked_edge_ids": [],
        }
    expected_hash = stable_hash(_artifact_body(artifact))
    if artifact.get("artifact_hash") != expected_hash:
        failures.append({"code": "artifact_hash_mismatch", "detail": "Graph Artifact hash mismatch."})
    if artifact.get("artifact_id") != f"qeg_{expected_hash[:24]}":
        failures.append({"code": "artifact_id_mismatch", "detail": "Graph Artifact ID mismatch."})
    expected_query_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
    if artifact.get("query_sha256") != expected_query_hash:
        failures.append({"code": "query_hash_mismatch", "detail": "Question hash mismatch."})

    rebuilt = build_question_evidence_graph(
        question,
        evidence_records,
        source_graph=source_graph,
        question_plan=question_plan,
        reference_date=reference_date,
    )
    if canonical_json(_artifact_body(artifact)) != canonical_json(_artifact_body(rebuilt)):
        failures.append({"code": "artifact_rebuild_mismatch", "detail": "Graph does not match current Evidence."})

    nodes = artifact.get("nodes", [])
    edges = artifact.get("edges", [])
    node_ids = [node.get("node_id") for node in nodes if isinstance(node, Mapping)]
    if len(node_ids) != len(set(node_ids)) or any(not node_id for node_id in node_ids):
        failures.append({"code": "node_ids_invalid", "detail": "Node IDs must be unique and non-empty."})
    edge_ids = [edge.get("edge_id") for edge in edges if isinstance(edge, Mapping)]
    if len(edge_ids) != len(set(edge_ids)) or any(not edge_id for edge_id in edge_ids):
        failures.append({"code": "edge_ids_invalid", "detail": "Edge IDs must be unique and non-empty."})
    known_nodes = set(node_ids)
    for edge in edges:
        if not isinstance(edge, Mapping):
            failures.append({"code": "edge_invalid", "detail": "Edge is not an object."})
            continue
        if edge.get("source") not in known_nodes or edge.get("target") not in known_nodes:
            failures.append({"code": "edge_endpoint_missing", "detail": str(edge.get("edge_id", ""))})
        basis = edge.get("basis")
        if not isinstance(basis, Mapping) or basis.get("kind") not in {"explicit", "inference"} or not basis.get("rule"):
            failures.append({"code": "edge_basis_missing", "detail": str(edge.get("edge_id", ""))})

    primary_path = artifact.get("primary_path")
    if artifact.get("status") == "ready":
        if not isinstance(primary_path, list) or len(primary_path) < 2:
            failures.append({
                "code": "primary_path_invalid",
                "detail": "Ready Graph must declare a non-empty primary path.",
            })
        else:
            missing_path_nodes = [
                node_id for node_id in primary_path if node_id not in known_nodes
            ]
            if missing_path_nodes:
                failures.append({
                    "code": "primary_path_node_missing",
                    "detail": str(missing_path_nodes[:6]),
                })
            edge_pairs = {
                (edge.get("source"), edge.get("target"))
                for edge in edges if isinstance(edge, Mapping)
            }
            for source, target in zip(primary_path, primary_path[1:]):
                if (source, target) not in edge_pairs:
                    failures.append({
                        "code": "primary_path_edge_missing",
                        "detail": f"{source} -> {target}",
                    })

    operation = (
        artifact.get("intent", {}).get("operation")
        if isinstance(artifact.get("intent"), Mapping) else None
    )
    if artifact.get("status") == "ready" and operation == "record_lookup":
        branches = artifact.get("branches")
        if not isinstance(branches, list) or not branches:
            failures.append({
                "code": "record_lookup_branches_missing",
                "detail": "Ready record lookup must declare one branch per requested item.",
            })
            branches = []
        edge_pairs = {
            (edge.get("source"), edge.get("target"))
            for edge in edges if isinstance(edge, Mapping)
        }
        branch_item_ids = []
        branch_selected_union: list[str] = []
        for branch in branches:
            if not isinstance(branch, Mapping):
                failures.append({
                    "code": "record_lookup_branch_invalid",
                    "detail": "Branch is not an object.",
                })
                continue
            item_id = str(branch.get("item_id", ""))
            branch_item_ids.append(item_id)
            branch_path = branch.get("primary_path")
            if not isinstance(branch_path, list) or len(branch_path) < 2:
                failures.append({
                    "code": "record_lookup_branch_path_invalid",
                    "detail": item_id,
                })
            else:
                for node_id in branch_path:
                    if node_id not in known_nodes:
                        failures.append({
                            "code": "record_lookup_branch_path_node_missing",
                            "detail": f"{item_id}: {node_id}",
                        })
                for source, target in zip(branch_path, branch_path[1:]):
                    if (source, target) not in edge_pairs:
                        failures.append({
                            "code": "record_lookup_branch_path_edge_missing",
                            "detail": f"{item_id}: {source} -> {target}",
                        })
            selected_ids = branch.get("selected_evidence_ids")
            validation_ids = branch.get("validation_evidence_ids")
            if not isinstance(selected_ids, list) or not selected_ids:
                failures.append({
                    "code": "record_lookup_branch_evidence_missing",
                    "detail": item_id,
                })
                selected_ids = []
            if not isinstance(validation_ids, list) or not validation_ids:
                failures.append({
                    "code": "record_lookup_branch_validation_missing",
                    "detail": item_id,
                })
                validation_ids = []
            branch_selected_union.extend(map(str, selected_ids))
            branch_binding = branch.get("stored_graph_binding")
            if not isinstance(branch_binding, Mapping):
                failures.append({
                    "code": "record_lookup_branch_binding_missing",
                    "detail": item_id,
                })
                continue
            required_ids = branch_binding.get("required_evidence_ids")
            if (
                not isinstance(required_ids, list)
                or set(map(str, validation_ids)) - set(map(str, required_ids))
            ):
                failures.append({
                    "code": "record_lookup_branch_binding_incomplete",
                    "detail": item_id,
                })
            branch_binding_body = {
                key: branch_binding[key]
                for key in branch_binding if key != "traversal_sha256"
            }
            if branch_binding.get("traversal_sha256") != stable_hash(branch_binding_body):
                failures.append({
                    "code": "record_lookup_branch_traversal_hash_mismatch",
                    "detail": item_id,
                })
            if source_graph is not None:
                for binding_key, graph_key in (
                    ("graph_sha256", "graph_sha256"),
                    ("partition_sha256", "partition_sha256"),
                    ("eligible_evidence_set_sha256", "eligible_evidence_set_sha256"),
                ):
                    if branch_binding.get(binding_key) != source_graph.get(graph_key):
                        failures.append({
                            "code": "record_lookup_branch_snapshot_mismatch",
                            "detail": f"{item_id}: {binding_key}",
                        })
                graph_relation_ids = {
                    str(edge.get("relation_id"))
                    for edge in source_graph.get("edges", [])
                    if isinstance(edge, Mapping)
                }
                traversed_relation_ids = branch_binding.get("traversed_relation_ids")
                if (
                    not isinstance(traversed_relation_ids, list)
                    or set(map(str, traversed_relation_ids)) - graph_relation_ids
                ):
                    failures.append({
                        "code": "record_lookup_branch_relation_unknown",
                        "detail": item_id,
                    })

        expected_plan = _record_lookup_plan(
            question_plan,
            activate_unknown=bool(_mentioned_record_fields(question)),
            question=question,
        )
        expected_item_ids = (
            [item["item_id"] for item in expected_plan]
            if expected_plan is not None else []
        )
        if branch_item_ids != expected_item_ids:
            failures.append({
                "code": "record_lookup_branch_plan_mismatch",
                "detail": str({
                    "expected": expected_item_ids,
                    "observed": branch_item_ids,
                }),
            })
        expected_union = list(dict.fromkeys(branch_selected_union))
        if list(map(str, artifact.get("selected_evidence_ids", []))) != expected_union:
            failures.append({
                "code": "record_lookup_selected_union_mismatch",
                "detail": "Top-level selected Evidence must be the ordered branch union.",
            })

    binding = artifact.get("stored_graph_binding")
    binding_required = operation in {"aggregate_count", "record_lookup"}
    if (
        artifact.get("status") == "ready"
        and operation == "record_lookup"
        and not isinstance(binding, Mapping)
    ):
        failures.append({
            "code": "stored_graph_binding_missing",
            "detail": "Record lookup overlay is not bound to the stored Graph.",
        })
    if source_graph is not None and binding_required:
        if not isinstance(binding, Mapping):
            if not (
                artifact.get("status") == "ready"
                and operation == "record_lookup"
            ):
                failures.append({
                    "code": "stored_graph_binding_missing",
                    "detail": "Question overlay is not bound to the stored Graph.",
                })
        else:
            binding_body = {
                key: binding[key] for key in binding if key != "traversal_sha256"
            }
            if binding.get("traversal_sha256") != stable_hash(binding_body):
                failures.append({
                    "code": "stored_graph_traversal_hash_mismatch",
                    "detail": "Stored Graph traversal hash mismatch.",
                })
            for binding_key, graph_key in (
                ("graph_sha256", "graph_sha256"),
                ("partition_sha256", "partition_sha256"),
                ("eligible_evidence_set_sha256", "eligible_evidence_set_sha256"),
            ):
                if binding.get(binding_key) != source_graph.get(graph_key):
                    failures.append({
                        "code": "stored_graph_snapshot_mismatch",
                        "detail": binding_key,
                    })
            graph_relation_ids = {
                str(edge.get("relation_id"))
                for edge in source_graph.get("edges", [])
                if isinstance(edge, Mapping)
            }
            traversed_relation_ids = binding.get("traversed_relation_ids")
            if (
                not isinstance(traversed_relation_ids, list)
                or set(map(str, traversed_relation_ids)) - graph_relation_ids
            ):
                failures.append({
                    "code": "stored_graph_relation_unknown",
                    "detail": "Traversal refers to an unknown stored relation.",
                })

    artifact_status = artifact.get("status")
    if artifact_status == "hold":
        failures.append({"code": "graph_hold", "detail": str(artifact.get("reason", ""))})
    elif artifact_status == "unsupported":
        warnings.append({"code": "graph_not_applicable", "detail": str(artifact.get("reason", ""))})
    elif artifact_status != "ready":
        failures.append({"code": "graph_status_invalid", "detail": str(artifact_status)})

    return {
        "validator_version": VALIDATOR_VERSION,
        "status": "blocked" if failures else ("not_applicable" if artifact_status == "unsupported" else "pass"),
        "failures": failures,
        "warnings": warnings,
        "checked_edge_ids": edge_ids,
        "artifact_hash": artifact.get("artifact_hash"),
        "selected_evidence_ids": list(artifact.get("selected_evidence_ids", [])),
        "resolved_value": (artifact.get("selection") or {}).get("value"),
    }
