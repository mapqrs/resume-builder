from resume_builder.formatting_check import check_template
from resume_builder.schema import Template


def test_bock_formatting_warns_on_current_compact_defaults():
    template = Template()
    warnings = check_template(template)
    rules = [w.rule for w in warnings]
    assert "bock-font-size" in rules
    assert rules.count("bock-margin") == 2
    assert any("left margin" in w.message for w in warnings)


def test_bock_formatting_accepts_11pt_and_half_inch_margins():
    template = Template()
    template.fonts.body.size = 11.0
    template.page.margin_left = "0.5in"
    template.page.margin_right = "0.5in"
    assert check_template(template) == []
