"""Structural protection must round-trip losslessly and detect damage."""

from nlab_pipeline.protector import Protector, protected_spans

BT = chr(96)


def test_roundtrip_preserves_everything():
    source = (
        "A [[presheaf]] on $C$ uses\n\n"
        "$$F: C\\to Set$$\n\n"
        "See [arXiv](http://arxiv.org/abs/0704.2030) and " + BT + "code" + BT + " <em>html</em>."
    )
    protector = Protector()
    tokenized = protector.protect(source)
    # Placeholders only, no raw structure left for the model.
    assert "$$" not in tokenized
    assert "[[presheaf]]" not in tokenized
    assert "http://arxiv.org" not in tokenized
    assert protector.restore(tokenized) == source


def test_roundtrip_preserves_multiline_math_and_latex_delimiters():
    source = (
        "Before\n\n$$\nF(x) = y\n$$\n\n"
        r"\[A \simeq B\] and \(x + y\) and "
        "\\begin{align*}a&=b\\end{align*}"
    )
    protector = Protector()
    tokenized = protector.protect(source)
    assert "F(x)" not in tokenized
    assert r"A \simeq B" not in tokenized
    assert protector.restore(tokenized) == source


def test_validation_detects_duplicate_and_reordered_tokens():
    protector = Protector()
    tokenized = protector.protect("$a$ then $b$")
    duplicated = tokenized + " @@PROT[0]@@"
    assert any("重复" in error for error in protector.validate_roundtrip(tokenized, duplicated))
    reordered = tokenized.replace("@@PROT[0]@@", "@@TMP@@").replace(
        "@@PROT[1]@@", "@@PROT[0]@@"
    ).replace("@@TMP@@", "@@PROT[1]@@")
    assert any("顺序" in error for error in protector.validate_roundtrip(tokenized, reordered))


def test_protects_link_target_but_leaves_label_translatable():
    protector = Protector()
    tokenized = protector.protect("See [descriptive label](https://example.test/a).")
    assert "descriptive label" in tokenized
    assert "https://example.test/a" not in tokenized
    assert protector.restore(tokenized) == "See [descriptive label](https://example.test/a)."


def test_protects_nested_parentheses_and_reference_ids():
    source = "See [paper](https://example.test/a_(b)) and [result][stable-id]."
    protector = Protector()
    tokenized = protector.protect(source)
    assert "https://example.test/a_(b)" not in tokenized
    assert "stable-id" not in tokenized
    assert protector.restore(tokenized) == source


def test_protected_spans_preserve_exact_structure():
    source = "Text $x$, `code`, and \\begin{align}a=b\\end{align}."
    assert protected_spans(source) == [
        ("math_inline", "$x$"),
        ("code", "`code`"),
        ("latex_env", "\\begin{align}a=b\\end{align}"),
    ]


def test_validation_detects_dropped_tokens():
    protector = Protector()
    tokenized = protector.protect("x [[a]] y $b$ z [c](http://d) e " + BT + "f" + BT)
    damaged = tokenized.replace("@@PROT[2]@@", "OOPS")
    errors = protector.validate_roundtrip(tokenized, damaged)
    assert any("丢失保护块" in e for e in errors)


def test_validation_detects_invented_tokens():
    protector = Protector()
    tokenized = protector.protect("plain text only")
    fake = tokenized + " @@PROT[99]@@"
    errors = protector.validate_roundtrip(tokenized, fake)
    assert any("未知保护块" in e or "越界" in e for e in errors)


def test_protect_exact():
    protector = Protector()
    token = protector.protect_exact("## 定义")
    assert protector.restore(token) == "## 定义"


def test_heading_normalisation():
    from nlab_pipeline.cleaner import normalize_headings

    assert normalize_headings("######Definition") == "###### Definition"
    assert normalize_headings("## Idea") == "## Idea"
