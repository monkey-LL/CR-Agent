"""Tests for extended deterministic rules engine."""

import pytest

from cr_agent.core.diff_parser import parse_diff
from cr_agent.core.models import Severity
from cr_agent.core.rules_engine import DETERMINISTIC_RULES, run_deterministic_checks


def make_diff(content: str, filename: str = "test.py") -> str:
    """Create a minimal diff with one added line."""
    return f"--- a/{filename}\n+++ b/{filename}\n@@ -1,1 +1,2 @@\n context\n+{content}\n"


class TestNewSecurityRules:
    def test_detects_exec_usage(self):
        hunks = parse_diff(make_diff("exec(compiled_code)"))
        findings = run_deterministic_checks(hunks)
        exec_findings = [f for f in findings if f.rule_id == "security.exec-usage"]
        assert len(exec_findings) == 1
        assert exec_findings[0].severity == Severity.BLOCKER

    def test_detects_pickle_deserialize(self):
        hunks = parse_diff(make_diff("data = pickle.loads(raw_data)"))
        findings = run_deterministic_checks(hunks)
        pickle_findings = [f for f in findings if f.rule_id == "security.pickledeserialize"]
        assert len(pickle_findings) == 1
        assert pickle_findings[0].severity == Severity.BLOCKER

    def test_detects_yaml_unsafe_load(self):
        hunks = parse_diff(make_diff("config = yaml.load(data)"))
        findings = run_deterministic_checks(hunks)
        yaml_findings = [f for f in findings if f.rule_id == "security.yaml-unsafe-load"]
        assert len(yaml_findings) == 1
        assert yaml_findings[0].severity == Severity.BLOCKER

    def test_allows_yaml_safe_load(self):
        hunks = parse_diff(make_diff("config = yaml.safe_load(data)"))
        findings = run_deterministic_checks(hunks)
        yaml_findings = [f for f in findings if f.rule_id == "security.yaml-unsafe-load"]
        assert len(yaml_findings) == 0

    def test_detects_shell_true(self):
        hunks = parse_diff(make_diff("result = subprocess.run(cmd, shell=True)"))
        findings = run_deterministic_checks(hunks)
        shell_findings = [f for f in findings if f.rule_id == "security.shell-true"]
        assert len(shell_findings) == 1
        assert shell_findings[0].severity == Severity.BLOCKER

    def test_detects_ssl_verify_false(self):
        hunks = parse_diff(make_diff("requests.post(url, verify=False)"))
        findings = run_deterministic_checks(hunks)
        verify_findings = [f for f in findings if f.rule_id == "security.verify-false"]
        assert len(verify_findings) == 1
        assert verify_findings[0].severity == Severity.MAJOR

    def test_detects_insecure_random(self):
        hunks = parse_diff(make_diff("token = random.randint(0, 999999)"))
        findings = run_deterministic_checks(hunks)
        random_findings = [f for f in findings if f.rule_id == "security.insecure-random"]
        assert len(random_findings) == 1
        assert random_findings[0].severity == Severity.MAJOR

    def test_detects_assert_sensitive(self):
        hunks = parse_diff(make_diff("assert user.password == expected"))
        findings = run_deterministic_checks(hunks)
        assert_findings = [f for f in findings if f.rule_id == "security.assert-sensitive"]
        assert len(assert_findings) == 1
        assert assert_findings[0].severity == Severity.MAJOR

    def test_detects_tempfile_mktemp(self):
        hunks = parse_diff(make_diff("path = tempfile.mktemp()"))
        findings = run_deterministic_checks(hunks)
        temp_findings = [f for f in findings if f.rule_id == "security.tempfile-race"]
        assert len(temp_findings) == 1
        assert temp_findings[0].severity == Severity.MAJOR


class TestErrorHandlingRules:
    def test_detects_bare_except(self):
        hunks = parse_diff(make_diff("except:"))
        findings = run_deterministic_checks(hunks)
        bare_findings = [f for f in findings if f.rule_id == "error-handling.bare-except"]
        assert len(bare_findings) == 1
        assert bare_findings[0].severity == Severity.MAJOR

    def test_detects_broad_except(self):
        hunks = parse_diff(make_diff("except Exception as e:"))
        findings = run_deterministic_checks(hunks)
        broad_findings = [f for f in findings if f.rule_id == "error-handling.broad-except"]
        assert len(broad_findings) == 1
        assert broad_findings[0].severity == Severity.MINOR


class TestMaintainabilityRules:
    def test_detects_mutable_default_list(self):
        hunks = parse_diff(make_diff("def foo(items=[]):"))
        findings = run_deterministic_checks(hunks)
        mutable_findings = [f for f in findings if f.rule_id == "maintainability.mutable-default"]
        assert len(mutable_findings) == 1
        assert mutable_findings[0].severity == Severity.MAJOR

    def test_detects_mutable_default_dict(self):
        hunks = parse_diff(make_diff("def foo(config={}):"))
        findings = run_deterministic_checks(hunks)
        mutable_findings = [f for f in findings if f.rule_id == "maintainability.mutable-default"]
        assert len(mutable_findings) == 1

    def test_detects_global_statement(self):
        hunks = parse_diff(make_diff("global counter"))
        findings = run_deterministic_checks(hunks)
        global_findings = [f for f in findings if f.rule_id == "maintainability.global-statement"]
        assert len(global_findings) == 1
        assert global_findings[0].severity == Severity.MINOR


class TestCodeQualityRules:
    def test_detects_import_star(self):
        hunks = parse_diff(make_diff("from os import *"))
        findings = run_deterministic_checks(hunks)
        star_findings = [f for f in findings if f.rule_id == "quality.import-star"]
        assert len(star_findings) == 1
        assert star_findings[0].severity == Severity.MINOR

    def test_detects_long_line(self):
        long_line = "x" * 130
        hunks = parse_diff(make_diff(long_line))
        findings = run_deterministic_checks(hunks)
        long_findings = [f for f in findings if f.rule_id == "quality.long-line"]
        assert len(long_findings) == 1
        assert long_findings[0].severity == Severity.INFO


class TestJavaScriptRules:
    def test_detects_innerhtml(self):
        hunks = parse_diff(make_diff("element.innerHTML = userInput", filename="app.js"))
        findings = run_deterministic_checks(hunks)
        innerhtml_findings = [f for f in findings if f.rule_id == "security.js-innerhtml"]
        assert len(innerhtml_findings) == 1
        assert innerhtml_findings[0].severity == Severity.MAJOR

    def test_detects_document_write(self):
        hunks = parse_diff(make_diff("document.write('<h1>hi</h1>')", filename="app.js"))
        findings = run_deterministic_checks(hunks)
        dw_findings = [f for f in findings if f.rule_id == "security.js-document-write"]
        assert len(dw_findings) == 1

    def test_detects_var_declaration(self):
        hunks = parse_diff(make_diff("var x = 10", filename="app.js"))
        findings = run_deterministic_checks(hunks)
        var_findings = [f for f in findings if f.rule_id == "quality.js-var-declaration"]
        assert len(var_findings) == 1


class TestTypeSafetyRules:
    def test_detects_any_annotation(self):
        hunks = parse_diff(make_diff("def process(data: Any) -> None:"))
        findings = run_deterministic_checks(hunks)
        any_findings = [f for f in findings if f.rule_id == "type-safety.any-annotation"]
        assert len(any_findings) == 1
        assert any_findings[0].severity == Severity.INFO


class TestRuleCompleteness:
    def test_all_rules_have_required_fields(self):
        for rule in DETERMINISTIC_RULES:
            assert "rule_id" in rule, f"Rule missing rule_id: {rule}"
            assert "pattern" in rule, f"Rule missing pattern: {rule}"
            assert "severity" in rule, f"Rule missing severity: {rule}"
            assert "message" in rule, f"Rule missing message: {rule}"
            assert "suggestion" in rule, f"Rule missing suggestion: {rule}"

    def test_rule_count_at_least_25(self):
        assert len(DETERMINISTIC_RULES) >= 25, f"Expected >= 25 rules, got {len(DETERMINISTIC_RULES)}"

    def test_all_rule_ids_unique(self):
        ids = [r["rule_id"] for r in DETERMINISTIC_RULES]
        assert len(ids) == len(set(ids)), "Duplicate rule_id found"

    def test_all_patterns_compile(self):
        import re
        for rule in DETERMINISTIC_RULES:
            try:
                re.compile(rule["pattern"])
            except re.error as e:
                pytest.fail(f"Invalid regex in {rule['rule_id']}: {e}")