from second_brain.markdown_engine import (
    SectionExists,
    SectionNotFound,
    WriteMode,
    append_to_section,
    apply_write,
    create_section,
    delete_section,
    parse,
    replace_section,
    slugify,
)

SAMPLE = """# Sample File

Intro paragraph that must survive every edit.

## Alpha

Alpha body line one.
Alpha body line two.

### Alpha One

Nested content.

## Beta

```python
## not a heading inside a fence
print("## also not a heading")
```

Beta tail.

## Services & Endpoints

| Service | URL |
|---|---|
| api | https://api.example.com |
"""


def test_parse_builds_heading_tree_and_ignores_fences():
    doc = parse(SAMPLE)
    assert doc.title == "Sample File"
    assert [s.heading for s in doc.sections] == [
        "Alpha",
        "Alpha One",
        "Beta",
        "Services & Endpoints",
    ]
    assert [s.level for s in doc.sections] == [2, 3, 2, 2]
    alpha = doc.require("Alpha")
    assert [c.heading for c in doc.children(alpha)] == ["Alpha One"]
    assert doc.parent(doc.require("alpha-one")).heading == "Alpha"
    assert "## not a heading" in doc.body(doc.require("Beta"))


def test_slugify_matches_plan_references():
    assert slugify("Services & Endpoints") == "services-endpoints"
    assert slugify("  Pricing Tiers ") == "pricing-tiers"
    assert slugify("Do & Don't") == "do-don-t"


def test_etag_is_stable_and_whitespace_insensitive():
    first = parse(SAMPLE)
    second = parse(SAMPLE.replace("Beta tail.", "Beta tail.   "))
    assert first.require("Beta").etag == second.require("Beta").etag
    assert first.etag == second.etag
    changed = parse(SAMPLE.replace("Beta tail.", "Beta changed."))
    assert changed.require("Beta").etag != first.require("Beta").etag
    assert changed.require("Alpha").etag == first.require("Alpha").etag
    assert changed.etag != first.etag


def test_replace_section_leaves_rest_byte_identical():
    new_text, section = replace_section(SAMPLE, "Beta", "Brand new beta body.")
    before = parse(SAMPLE)
    after = parse(new_text)
    assert section.heading == "Beta"
    assert after.body(after.require("Beta")) == "Brand new beta body."
    for name in ("Alpha", "Alpha One", "Services & Endpoints"):
        assert before.body(before.require(name)) == after.body(after.require(name))
        assert before.require(name).etag == after.require(name).etag
    assert new_text.startswith("# Sample File\n\nIntro paragraph that must survive every edit.\n")
    assert new_text.endswith("| api | https://api.example.com |\n")


def test_replace_parent_keeps_nested_children():
    new_text, _ = replace_section(SAMPLE, "Alpha", "Only this line.")
    doc = parse(new_text)
    alpha = doc.require("Alpha")
    assert doc.body(alpha).startswith("Only this line.")
    assert doc.find("Alpha One") is not None
    assert doc.body(doc.require("Alpha One")) == "Nested content."


def test_append_create_delete():
    text, section = append_to_section(SAMPLE, "Beta", "- appended line")
    assert parse(text).body(section).endswith("- appended line")
    text, created = create_section(text, "Gamma", "Gamma body", level=2)
    assert created.level == 2 and created.slug == "gamma"
    assert parse(text).sections[-1].heading == "Gamma"
    text, child = create_section(text, "Alpha Two", "Second child", level=3, parent="Alpha")
    doc = parse(text)
    assert child.level == 3
    assert [c.heading for c in doc.children(doc.require("Alpha"))] == ["Alpha One", "Alpha Two"]
    text = delete_section(text, "Alpha One")
    doc = parse(text)
    assert doc.find("Alpha One") is None
    assert doc.find("Alpha Two") is not None


def test_errors():
    import pytest

    with pytest.raises(SectionNotFound):
        replace_section(SAMPLE, "Missing", "x")
    with pytest.raises(SectionExists):
        create_section(SAMPLE, "Alpha", "x")
    text, _ = apply_write(SAMPLE, "Beta", "via apply", WriteMode.REPLACE)
    assert parse(text).body(parse(text).require("Beta")) == "via apply"


def test_crlf_input_is_normalised():
    doc = parse(SAMPLE.replace("\n", "\r\n"))
    assert doc.etag == parse(SAMPLE).etag
    assert "\r" not in doc.text
