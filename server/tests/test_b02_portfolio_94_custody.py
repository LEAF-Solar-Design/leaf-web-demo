"""B02: real save/read/export functions with controlled SQL and preview storage.

Synthetic bytes only. This does not prove a database, UI, model inspection,
image decoding, native launch or LIVE acceptance.
"""
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import uuid
import zipfile

import pytest
import platform_link

platform_link._ensure_platform_package()
from leaf_platform import project_lifecycle as lifecycle, project_web_page as pages
import project_material_images as images


@pytest.mark.parametrize('target', ['web', 'ios'])
def test_94_saved_assignments_reload_and_historical_source(monkeypatch, target):
    org, project, actor = (str(uuid.uuid4()) for _ in range(3))
    previews, materials = {}, {}
    assigned = []
    for index in range(94):
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'https://fixture.invalid/b02/{index}'))
        previews[mid] = f'synthetic preview {index}'.encode()
        materials[mid] = dict(material_id=mid,
            sha256=hashlib.sha256(f'synthetic original {index}'.encode()).hexdigest(),
            preview_sha256=hashlib.sha256(previews[mid]).hexdigest())
        assigned.append({**materials[mid], 'title': f'Photo {index}', 'alt': f'Synthetic photo {index}'})
    document = dict(schema='leaf.portfolio.v1', name='Synthetic portfolio', headline='Selected work',
        introduction='', about='', contact=dict(email='', location=''), resume=[], theme='light',
        categories=[dict(slug='work', title='Work', description='', hero=0, images=assigned)])
    revisions, reads = {}, []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, args):
            assert args[:2] == (org, project)
            if 'FROM project_materials' in sql:
                self.row = copy.deepcopy(materials.get(args[2]))
            elif 'SELECT revision,digest,document' in sql:
                revision = args[2] if len(args) > 2 else None
                self.row = copy.deepcopy(revisions.get(revision or max(revisions, default=0)))
            else:
                assert sql.startswith('INSERT INTO project_web_page_revisions ')
                assert args[5] == actor
                revisions[args[2]] = dict(revision=args[2], document=copy.deepcopy(args[3].obj), digest=args[4])

        def fetchone(self):
            return self.row

    def authorize(*args, write=False):
        assert args == (org, project, actor)
        return 'owner'

    def read_content(o, p, row, variant):
        assert (o, p, variant) == (org, project, 'preview')
        mid = row['material_id']
        reads.append(mid)
        return previews[mid]

    monkeypatch.setattr(pages, 'run_transaction', lambda operation, **kwargs: operation(SimpleNamespace(cursor=Cursor)))
    monkeypatch.setattr(pages, '_authorize', lambda cur, *args, **kwargs: authorize(*args, **kwargs))
    monkeypatch.setattr(lifecycle, 'require_project_role', authorize)
    monkeypatch.setattr(images, 'read_content', read_content)
    assert pages.read(org, project, actor) is None
    saved = pages.save(org, project, actor, expected_revision=0, document=document)
    assert saved['revision'] == 1
    assert pages.read(org, project, actor) == saved
    assert pages.save(org, project, actor, expected_revision=0, document=document) == saved
    changed = copy.deepcopy(document)
    changed['headline'] = 'Later draft'
    assert pages.save(org, project, actor, expected_revision=1, document=changed)['revision'] == 2
    with pytest.raises(lifecycle.LifecycleConflict):
        pages.save(org, project, actor, expected_revision=0, document=document)
    exported, raw = pages.export_source(org, project, actor, revision=1, target=target)
    assert exported == saved
    assert len(reads) == 94 and set(reads) == set(materials)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prefix = 'Portfolio/PortfolioContent/' if target == 'ios' else ''
        assert json.loads(archive.read(prefix + 'portfolio.json')) == document
        image_prefix = prefix + 'images/' if target == 'ios' else 'public/images/'
        archived = {name for name in archive.namelist() if name.startswith(image_prefix)}
        assert archived == {image_prefix + mid + '.jpg' for mid in materials}
        for mid, preview in previews.items():
            assert archive.read(image_prefix + mid + '.jpg') == preview
        manifest = json.loads(archive.read('source-manifest.json'))
        assert manifest['revision'] == saved['revision']
        assert pages.read(org, project, actor, revision=1) == saved
