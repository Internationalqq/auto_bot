import pytest
from reconcile_browser_instructions import reconcile,OLD_SECTIONS


@pytest.mark.parametrize('profile',['gulya','commercial'])
def test_replaces_stale_routes_preserving_other_sections(profile):
    source=OLD_SECTIONS[0]+'\n\nOld mandatory visible browser.\n\n# Role\nKeep role and prices.\n\n'+OLD_SECTIONS[2]+'\n\nOld sending route.\n\n## No duplicates\nKeep history.\n'
    result=reconcile(source,profile)
    assert 'Old mandatory' not in result and 'Old sending' not in result
    assert '# Role\nKeep role and prices.' in result
    assert '## No duplicates\nKeep history.' in result
    assert 'browser_*' in result and 'не обходить' in result
    assert reconcile(result,profile)==result


def test_other_profiles_not_mutated():
    with pytest.raises(ValueError):reconcile('unchanged','someone_else')
