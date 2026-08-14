"""Structural protection must round-trip losslessly and detect damage."""

from nlab_pipeline.protector import Protector

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
