import hashlib

from agents.frontmatter import parse_frontmatter
from agents.skill_evolution import skill_content_hash
from agents.skills import _parse_skill_file


def test_exact_byte_binding_preserves_cross_platform_semantic_hash(tmp_path):
    text = '---\nname: example\ndescription: Example\n---\n# Steps\n\nReturn a conclusion.\n'
    path = tmp_path / 'SKILL.md'
    raw = text.replace('\n', '\r\n').encode('utf-8')
    path.write_bytes(raw)
    skill = _parse_skill_file(path, 'project', str(tmp_path))
    parsed = parse_frontmatter(text)
    expected = skill_content_hash({'name': parsed.meta['name'],
        'description': parsed.meta['description'], 'when_to_use': '', 'instructions': parsed.body})
    actual = skill_content_hash({'name': skill.name, 'description': skill.description,
        'when_to_use': skill.when_to_use, 'instructions': skill.prompt_template})
    assert actual == expected
    assert skill.content_sha256 == hashlib.sha256(raw).hexdigest()
