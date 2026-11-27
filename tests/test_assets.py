from app.assets import asset_version


def test_asset_version_changes_when_content_changes_even_with_same_size(tmp_path):
    script = tmp_path / 'editor.js'
    script.write_text('old', encoding='utf-8')
    previous = asset_version(tmp_path)
    assert asset_version(tmp_path) == previous
    script.write_text('new', encoding='utf-8')
    assert asset_version(tmp_path) != previous


def test_added_stylesheet_changes_asset_version(tmp_path):
    (tmp_path / 'editor.js').write_text('script', encoding='utf-8')
    previous = asset_version(tmp_path)
    (tmp_path / 'editor-changes.css').write_text('styles', encoding='utf-8')
    assert asset_version(tmp_path) != previous
