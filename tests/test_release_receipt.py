import copy
import json
from pathlib import Path
import pytest
from trader_engine.operations.release_receipt import build_receipt, verify_receipt, save_receipt


def fixture(tmp_path,clean=True):
    (tmp_path/'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (tmp_path/'requirements.txt').write_text('pytest\n')
    config=dict(repo_root=str(tmp_path),python=str(tmp_path/'python'),state_root=str(tmp_path/'state'))
    inspect=lambda _:dict(checks=[dict(name='dependencies',status='pass',detail={'pytest':'8.0'}),dict(name='single_writer',status='warning')])
    identity=lambda _:dict(commit='a'*40,working_tree_clean=clean)
    return config,inspect,identity


def test_dirty_source_never_eligible_and_no_activation(tmp_path):
    config,inspect,identity=fixture(tmp_path,False)
    receipt=build_receipt(config,inspect=inspect,identity=identity)
    assert not receipt['eligible_for_staging'] and not receipt['execution_authorized']
    assert 'uncommitted_source_changes' in receipt['checks_failed']


def test_config_dependencies_commit_changes_invalidate_receipt(tmp_path):
    config,inspect,identity=fixture(tmp_path)
    receipt=build_receipt(config,inspect=inspect,identity=identity)
    assert verify_receipt(receipt,receipt)['matches']
    for field,value in [('manifest_digest','changed'),('installed_dependencies',{}),('source',{})]:
        altered=copy.deepcopy(receipt);altered[field]=value
        assert not verify_receipt(receipt,altered)['matches']


def test_source_and_secret_values_are_not_exported(tmp_path):
    config,inspect,identity=fixture(tmp_path);config['UNEXPECTED_SECRET']='must-not-be-exported'
    receipt=build_receipt(config,inspect=inspect,identity=identity)
    assert 'must-not-be-exported' not in json.dumps(receipt)
    assert receipt['checks_unverified']==['single_writer']


def test_receipt_refuses_overwrite(tmp_path):
    path=tmp_path/'receipt.json';save_receipt(path,{'version':1})
    with pytest.raises(FileExistsError):save_receipt(path,{'version':2})
    assert json.loads(path.read_text())['version']==1


@pytest.mark.parametrize("final", [dict(commit="b"*40,working_tree_clean=True),dict(commit="a"*40,working_tree_clean=False)])
def test_source_change_during_checks_blocks_receipt(tmp_path, final):
    config,inspect,_=fixture(tmp_path)
    identities=iter([dict(commit="a"*40,working_tree_clean=True),final])
    receipt=build_receipt(config,inspect=inspect,identity=lambda _:next(identities))
    assert not receipt['eligible_for_staging']
    assert 'source_changed_during_checks' in receipt['checks_failed']
