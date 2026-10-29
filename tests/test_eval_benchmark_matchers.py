"""eval/run_benchmark.py 判分器回归测试（纯函数 + 零模型）。

覆盖判分缺陷修复：
- hle_match（multipleChoice）：取 Final Answer 后首个选项字母，而非把解释里的
  最后一个 A-H 字母当答案（'C. Both A and B qualify.' 应判 C 对、B 错）；
  选项字母范围放宽到 A-Z，覆盖 HLE 真实 9 项以上的多选（gold ∈ {I,J,L,M,V,P}）。
- gaia_match 数值：默认精确相等，仅当 gold 含近似/约等标记时才允许 1% 相对容差
  （预测侧加 '~' 不放宽判分）；gold 含 'about/~' 修饰词时剥离修饰词后与纯数值判等。
- _normalize：仅对多词文本删独立冠词，单词答案（'A'/'a'/'the'）不被归一成空串。
- score()：pass@1 只在可判分记录（未超时/无错误/有 Final Answer）上计算，未跑记录
  不计入分母但保留计数；judge 校准字段仅在显式 --judge-calibration 时写入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import eval.run_benchmark as rb  # noqa: E402
from eval.run_benchmark import (  # noqa: E402
    _is_scorable,
    _normalize,
    extract_final_answer,
    gaia_match,
    hle_match,
)


# ── finding 1: hle_match multipleChoice ──────────────────────────────────────

def test_hle_mc_takes_leading_letter_not_last():
    pred = extract_final_answer("... Final Answer: C. Both A and B qualify.")
    assert hle_match(pred, "C", "multipleChoice") is True
    assert hle_match(pred, "B", "multipleChoice") is False
    assert hle_match(pred, "A", "multipleChoice") is False


def test_hle_mc_accepts_common_formats():
    assert hle_match("B", "B", "multipleChoice") is True
    assert hle_match("b", "B", "multipleChoice") is True
    assert hle_match("(D) because ...", "D", "multipleChoice") is True
    assert hle_match("答案C", "C", "multipleChoice") is True
    assert hle_match("The correct option is C", "C", "multipleChoice") is True


def test_hle_mc_ignores_answer_word_letter():
    # 'ANSWER' 里的 A 不应被当作选项
    assert hle_match("ANSWER: D", "D", "multipleChoice") is True
    # 无选项字母 → 判错
    assert hle_match("none of these", "A", "multipleChoice") is False


def test_hle_exact_match_uses_normalize():
    assert hle_match("The Cat", "cat", "exactMatch") is True
    assert hle_match("42", "42", "exactMatch") is True
    assert hle_match("", "42", "exactMatch") is False


def test_hle_mc_supports_option_letters_beyond_h():
    # HLE 真实多选可达 9 项以上，gold 可为 I/J/L/M/V/P；旧 A-H 上限使这些越界 gold 恒判错。
    assert hle_match("I", "I", "multipleChoice") is True
    assert hle_match("i", "I", "multipleChoice") is True
    assert hle_match("J.", "J", "multipleChoice") is True
    assert hle_match("L) because ...", "L", "multipleChoice") is True
    assert hle_match("M", "M", "multipleChoice") is True
    assert hle_match("P", "P", "multipleChoice") is True
    assert hle_match("V", "V", "multipleChoice") is True
    # 错答 / 越界 gold 但预测未给字母 → 仍判错
    assert hle_match("I", "J", "multipleChoice") is False
    assert hle_match("none of these", "J", "multipleChoice") is False


# ── finding 2: gaia_match 数值容差 ───────────────────────────────────────────

def test_gaia_numeric_requires_exact_by_default():
    assert gaia_match("1005000", "1000000") is False
    assert gaia_match("1001000", "1000000") is False


def test_gaia_numeric_equal_after_formatting():
    assert gaia_match("1,000,000", "1000000") is True
    assert gaia_match("3.0", "3") is True


def test_gaia_numeric_tolerance_only_with_approx_marker():
    assert gaia_match("~1005000", "~1000000") is True
    assert gaia_match("~10.05", "~10") is True
    assert gaia_match("10", "10.05") is False


def test_gaia_numeric_matches_gold_with_modifier():
    # gold 含修饰词（about/~/around）时，纯数值预测应判等（此前残余不一致被误判为错）。
    assert gaia_match("100", "about 100") is True
    assert gaia_match("100", "~100") is True
    assert gaia_match("1,000,000", "around 1000000") is True
    # 单位不同仍判错（残余不一致）
    assert gaia_match("100 meters", "about 100 seconds") is False


def test_gaia_prediction_side_approx_marker_does_not_enable_tolerance():
    # 仅 gold 侧标记启用容差：预测侧自行加 '~' 不能放宽判分（否则 pass@1 虚高）。
    assert gaia_match("~1005000", "1000000") is False
    assert gaia_match("~1050000", "1000000") is False
    # gold 侧标记仍启用 1% 容差。
    assert gaia_match("1005000", "~1000000") is True


# ── finding 3: _normalize 冠词删除 ───────────────────────────────────────────

def test_normalize_keeps_single_token_answer():
    assert _normalize("A") == "a"
    assert _normalize("a") == "a"
    assert _normalize("A.") == "a"
    assert _normalize("the") == "the"


def test_normalize_still_drops_articles_in_phrases():
    assert _normalize("The Cat") == "cat"
    assert _normalize("a cat") == "cat"


def test_single_letter_gold_no_longer_always_wrong():
    assert gaia_match("A", "A") is True
    assert gaia_match("A.", "a") is True


# ── finding 4: score() 分母口径 ──────────────────────────────────────────────

def test_is_scorable_filters_unfinished_records():
    assert _is_scorable({"has_final_marker": True}) is True
    assert _is_scorable({"has_final_marker": True, "timed_out": True}) is False
    assert _is_scorable({"has_final_marker": True, "error": "watchdog"}) is False
    assert _is_scorable({"has_final_marker": False}) is False


def test_score_freezes_denominator_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "RUNS_DIR", tmp_path)
    tag_dir = tmp_path / "gaia"
    tag_dir.mkdir()

    def rec(prediction, gold, **over):
        base = {
            "prediction": prediction,
            "gold": gold,
            "folds": 0,
            "tokens": {"input": 10, "output": 5},
            "has_final_marker": True,
            "timed_out": False,
            "error": "",
            "problem_type": "p",
            "level": 1,
            "answer_type": "",
        }
        base.update(over)
        return base

    records = [
        rec("cat", "cat"),                       # 可判分且对
        rec("dog", "cat"),                       # 可判分且错
        rec("cat", "cat", timed_out=True),       # 超时 → 失败（计入主指标分母）
        rec("cat", "cat", has_final_marker=False),  # 无标记 → 失败（计入主指标分母）
    ]
    (tag_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )

    rb.score("gaia")

    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["count"] == 4
    assert summary["planned"] == 4
    assert summary["scorable"] == 2
    # 主指标计入失败：1 正确 / 4 计划 = 25%。
    assert summary["pass_at_1"] == 25.0
    # 有效作答准确率单列：1 正确 / 2 可判分 = 50%。
    assert summary["valid_answer_accuracy"] == 50.0
    assert summary["timed_out"] == 1
    assert summary["no_final_marker"] == 1
    # 冻结产物：summary.json 指向唯一派生物（含预测哈希与判分器版本）。
    assert "denominator_policy" in summary
    assert summary["provenance"]["predictions_sha256"]
    assert summary["provenance"]["judge_version"] == rb.JUDGE_VERSION
    # 不可变派生产物已落盘且与 summary 指向同一 score_run_id。
    score_file = tag_dir / "scores" / f"{summary['provenance']['score_run_id']}.json"
    assert score_file.is_file()
    # 重评分不覆盖原派生物：再跑一次 score 会生成新的 score_run_id。
    rb.score("gaia")
    again = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert again["provenance"]["score_run_id"] != summary["provenance"]["score_run_id"]
    assert len(list((tag_dir / "scores").glob("*.json"))) == 2


# ── finding 5: judge 校准 opt-in 入口（默认关闭） ────────────────────────────

GOLDEN_PAIRS = (
    Path(__file__).resolve().parents[1]
    / "eval"
    / "judge_calibration"
    / "golden_pairs.example.json"
)


def test_judge_calibration_entry_point_computes_kappa():
    from eval.run_benchmark import judge_calibration

    out = judge_calibration(GOLDEN_PAIRS)
    assert out["n_pairs"] == 6
    assert out["kappa"] == 1.0
    assert "swap" not in out  # 该 golden 文件无 swap 段，不应产生对应字段


def test_score_writes_judge_calibration_only_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "RUNS_DIR", tmp_path)
    tag_dir = tmp_path / "gaia"
    tag_dir.mkdir()
    record = {
        "prediction": "cat",
        "gold": "cat",
        "folds": 0,
        "tokens": {"input": 10, "output": 5},
        "has_final_marker": True,
        "timed_out": False,
        "error": "",
        "problem_type": "p",
        "level": 1,
        "answer_type": "",
    }
    (tag_dir / "predictions.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rb.score("gaia")  # 默认：不写 judge 校准字段
    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert "judge_calibration" not in summary

    rb.score("gaia", judge_calibration_path=str(GOLDEN_PAIRS))  # 显式 opt-in
    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["judge_calibration"]["kappa"] == 1.0
    assert summary["judge_calibration"]["n_pairs"] == 6


# ── finding 6(新): hle_match 只认「作为答案本身」的字母 ──────────────────────

def test_hle_mc_ignores_prose_pronoun_letter():
    # 'I think the answer is B'：'I' 是散文里的独立字母，不应被当作首个选项。
    assert hle_match("I think the answer is B", "B", "multipleChoice") is True
    assert hle_match("I think the answer is B", "I", "multipleChoice") is False
    # 罗马数字/代词等独立大写字母出现在答案句前后同样不应误判。
    assert hle_match("I am sure the answer is D.", "D", "multipleChoice") is True


def test_hle_mc_still_accepts_answer_shaped_letters():
    assert hle_match("B", "B", "multipleChoice") is True
    assert hle_match("C.", "C", "multipleChoice") is True
    assert hle_match("(D) because ...", "D", "multipleChoice") is True
    assert hle_match("answer: E", "E", "multipleChoice") is True
    assert hle_match("The correct option is F", "F", "multipleChoice") is True


def test_hle_mc_accepts_marker_with_is_are_then_colon():
    # 「显式标记 + IS/ARE + 冒号」连用此前漏判（IS/ARE 与冒号只取其一）。
    assert hle_match("The answer is: B", "B", "multipleChoice") is True
    assert hle_match("The correct option is: C", "C", "multipleChoice") is True
    assert hle_match("Answer is : D", "D", "multipleChoice") is True
    assert hle_match("The answer is: B", "A", "multipleChoice") is False


def test_hle_mc_accepts_fullwidth_parenthesized_option():
    # 全角括号选项此前漏判。
    assert hle_match("C）", "C", "multipleChoice") is True
    assert hle_match("（C）", "C", "multipleChoice") is True
    assert hle_match("答案：E）", "E", "multipleChoice") is True
    assert hle_match("（C）", "D", "multipleChoice") is False


# ── finding 7(新): 中文近似词加 CJK 词界 ─────────────────────────────────────

def test_approx_re_cjk_word_boundary():
    assert rb._APPROX_RE.search("纽约") is None   # 词内 '约' 不触发
    assert rb._APPROX_RE.search("条约") is None
    assert rb._APPROX_RE.search("约100") is not None
    assert rb._APPROX_RE.search("大约100") is not None
    assert rb._APPROX_RE.search("近似100") is not None


def test_gaia_cjk_word_not_treated_as_approx_marker():
    # '纽约' 里的 '约' 不应启用 1% 容差；此处 0.5% 差异应仍判错。
    assert gaia_match("1005000", "纽约 1000000") is False
    # 真正的近似词仍启用容差。
    assert gaia_match("100.5", "约100") is True
    assert gaia_match("100", "约100") is True


# ── finding 8(新): _APPROX_RE 补全近似标记 ───────────────────────────────────
# 旧集合缺 左右 / circa / approx. → gold 含这些标记时 1% 容差不启用，'100' vs
# '100左右' / 'circa 100' / 'approx. 100' 被误判为错。

def test_approx_re_includes_missing_markers():
    assert rb._APPROX_RE.search("100左右") is not None
    assert rb._APPROX_RE.search("左右 100") is not None
    assert rb._APPROX_RE.search("circa 100") is not None
    assert rb._APPROX_RE.search("approx. 100") is not None
    assert rb._APPROX_RE.search("approx 100") is not None
    # 'approximately' 为不同词，仍单独命中（不被 \bapprox\b 的边界吞掉）。
    assert rb._APPROX_RE.search("approximately 100") is not None


def test_gaia_numeric_tolerance_with_new_approx_markers():
    assert gaia_match("100", "100左右") is True
    assert gaia_match("100.5", "100左右") is True
    assert gaia_match("100", "circa 100") is True
    assert gaia_match("100", "approx. 100") is True
    # 无近似标记时仍要求精确，容差不得被这些词的新增而放宽。
    assert gaia_match("1005000", "1000000") is False


# ── finding 9(新): hle_match exactMatch 数值归一 ─────────────────────────────
# 旧实现仅 _normalize → '42.0' vs '42'、'1,000' vs '1000' 判错；与 gaia 口径不一致。

def test_hle_exact_match_numeric_normalization():
    assert hle_match("42.0", "42", "exactMatch") is True
    assert hle_match("42", "42.0", "exactMatch") is True
    assert hle_match("1,000", "1000", "exactMatch") is True
    assert hle_match("3.0", "3", "exactMatch") is True
    # 非数值仍按规范化字符串判等，不同数值/文本仍判错。
    assert hle_match("The Cat", "cat", "exactMatch") is True
    assert hle_match("43", "42", "exactMatch") is False
    assert hle_match("dog", "cat", "exactMatch") is False
    assert hle_match("", "42", "exactMatch") is False


# ── finding 10(新): hle 口径变更须写入报告 scoring_note ───────────────────────
# exactMatch 复用 gaia_match 后口径放宽，跨版本 pass@1 不可比；score() 应在 hle 报告里
# 显式标注该提示。

def test_score_writes_hle_scoring_note(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "RUNS_DIR", tmp_path)
    tag_dir = tmp_path / "hle"
    tag_dir.mkdir()
    record = {
        "prediction": "42.0",
        "gold": "42",
        "folds": 0,
        "tokens": {"input": 10, "output": 5},
        "has_final_marker": True,
        "timed_out": False,
        "error": "",
        "problem_type": "p",
        "answer_type": "exactMatch",
    }
    (tag_dir / "predictions.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rb.score("hle")

    summary = json.loads((tag_dir / "summary.json").read_text(encoding="utf-8"))
    assert "scoring_note" in summary
    assert "不可直接比较" in summary["scoring_note"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
