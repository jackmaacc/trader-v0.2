import json
import pytest
from trader_engine.data.evidence import EvidenceRecorder, verify_archive


def archive(tmp_path):
    root = tmp_path.resolve() / 'evidence'
    recorder = EvidenceRecorder(root, fetcher=lambda kind, params: {'bars': {}})
    recorder.request('bars', {})
    recorder.finish({'purpose':'offline_fixture'}, False)
    return recorder


def test_finalization_blocks_rewrite_and_later_acquisition(tmp_path):
    recorder = archive(tmp_path)
    original = (recorder.output/'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        recorder.finish({}, True)
    with pytest.raises(ValueError, match='finalized'):
        recorder.request('bars', {})
    assert (recorder.output/'manifest.json').read_bytes() == original
    report = verify_archive(recorder.output)
    assert report['integrity_verified'] and not report['acquisition_complete']
    assert not report['qualification_allowed']


@pytest.mark.parametrize('mutation', ['corrupt','missing','extra','symlink'])
def test_verify_detects_altered_inventory(tmp_path,mutation):
    root = archive(tmp_path).output
    page=root/'raw/page-000000.json'
    if mutation=='corrupt': page.write_text('{}')
    elif mutation=='missing': page.unlink()
    elif mutation=='extra': (root/'extra').write_text('extra')
    else:
        page.unlink();page.symlink_to(root/'lineage.jsonl')
    with pytest.raises(ValueError): verify_archive(root)


def test_inventory_cannot_escape_root(tmp_path):
    root=archive(tmp_path).output
    p=root/'manifest.json';data=json.loads(p.read_text())
    data['hashes']={'../outside':'0'*64};p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Unsafe'): verify_archive(root)
