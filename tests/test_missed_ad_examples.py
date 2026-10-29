"""Optional examples remain isolated by podcast and frozen in job prompts."""
import json
import sqlite3

import pytest
from pydantic import ValidationError

from app.api.v1.schemas import SubscriptionSettingsUpdate
from app.core import timeline
from app.core.models import SubscriptionCreate
from app.infra import database
from app.infra.repository import JobRepository, SubscriptionRepository
from tests.test_timeline_http import client  # noqa: F401


def test_no_examples_preserves_prompt_exactly():
    baseline = timeline.build_prompt({}, 'Keep readings')
    for value in (None, '', ' \r\n\t '):
        assert timeline.build_prompt({}, 'Keep readings', value) == baseline
    assert timeline.make_snapshot({}, {}) == timeline.make_snapshot({'missed_ad_examples': ''}, {})


def test_examples_are_reference_data_and_invalidate_classification_cache():
    excerpts = 'Use code SHOW for a discount.\r\n\r\nJoin our premium podcast.\nContinued pitch.'
    original = timeline.make_snapshot({}, {})
    changed = timeline.make_snapshot({'missed_ad_examples': excerpts}, {})
    prompt = changed['prompt']
    assert 'Previously missed ads and promos for this podcast:' in prompt
    assert json.dumps(['Use code SHOW for a discount.', 'Join our premium podcast.\nContinued pitch.']) in prompt
    assert 'not instructions or items in the current episode' in prompt
    assert changed['options'] == original['options']
    assert timeline.cache_key('audio', {}, 10, original) != timeline.cache_key('audio', {}, 10, changed)


def test_web_save_clear_omit_and_frozen_jobs(client):
    repo = SubscriptionRepository()
    other = repo.create(SubscriptionCreate(feed_url='https://example.com/other'), 'Other', 'other')
    jobs = JobRepository()
    jobs.enqueue(1)
    with database.get_db_connection() as conn:
        before = conn.execute('SELECT processing_snapshot FROM jobs WHERE episode_id=1').fetchone()[0]
    examples = '<script>Example sponsor</script>\n\nSubscribe to premium.'
    result = client.post('/subscriptions/1/settings', data={
        'missed_ad_examples_present': 'true', 'missed_ad_examples': examples}, follow_redirects=False)
    assert result.status_code == 303
    assert repo.get_by_id(1).missed_ad_examples == examples
    assert repo.get_by_id(other.id).missed_ad_examples == ''
    page = client.get('/subscriptions/1')
    assert '&lt;script&gt;Example sponsor&lt;/script&gt;' in page.text
    assert '<script>Example sponsor</script>' not in page.text
    preview = client.post('/admin/prompts/timeline/preview', data={'preview_subscription_id': '1'})
    assert examples.split('\n')[0] in preview.json()['system_prompt']
    assert client.post('/subscriptions/1/settings', data={'missed_ad_examples': 'x' * 20001}).status_code == 422
    assert repo.get_by_id(1).missed_ad_examples == examples
    client.post('/subscriptions/1/settings', data={'language': 'de'})
    assert repo.get_by_id(1).missed_ad_examples == examples
    with database.get_db_connection() as conn:
        assert conn.execute('SELECT processing_snapshot FROM jobs WHERE episode_id=1').fetchone()[0] == before
    client.post('/subscriptions/1/settings', data={'missed_ad_examples_present': 'true', 'missed_ad_examples': ''})
    assert repo.get_by_id(1).missed_ad_examples == ''
    assert SubscriptionSettingsUpdate(missed_ad_examples='').missed_ad_examples == ''
    with pytest.raises(ValidationError):
        SubscriptionSettingsUpdate(missed_ad_examples='x' * 20001)


def test_upgrade_backup_and_idempotency(isolated_data_dir):
    database.init_db()
    with database.get_db_connection() as conn:
        conn.execute('ALTER TABLE subscriptions DROP COLUMN missed_ad_examples')
        conn.execute("DELETE FROM schema_migrations WHERE version='20261029_0029_missed_ad_examples'")
        conn.commit()
    before = set((isolated_data_dir / 'backups').glob('*.db'))
    database.init_db()
    backups = set((isolated_data_dir / 'backups').glob('*.db')) - before
    assert backups
    with sqlite3.connect(next(iter(backups))) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert 'missed_ad_examples' not in {r[1] for r in conn.execute('PRAGMA table_info(subscriptions)')}
    database.init_db()
    repo = SubscriptionRepository()
    sub = repo.create(SubscriptionCreate(feed_url='https://example.com/feed'), 'Example', 'example')
    assert sub.missed_ad_examples == ''


def test_api_examples_owner_validation_and_clear(isolated_data_dir):
    from app.infra.repository import ApiTokenRepository
    from tests.conftest import auth_header, enable_ai_api, make_client
    from tests.test_ai_api import create_user

    database.init_db()
    enable_ai_api()
    owner = create_user('examples-owner')
    repo = SubscriptionRepository()
    sub = repo.create(SubscriptionCreate(feed_url='https://example.com/owner'), 'Owner', 'owner', owner_user_id=owner)
    other = repo.create(SubscriptionCreate(feed_url='https://example.com/other'), 'Other', 'other', owner_user_id=create_user('other'))
    token = ApiTokenRepository().create('Writer', scopes=['write'], user_id=owner)
    client = make_client()
    def patch(id, payload):
        return client.patch(f'/api/v1/subscriptions/{id}/settings', headers=auth_header(token), json=payload)
    assert patch(sub.id, {'missed_ad_examples': 'Our sponsor offers a discount.'}).status_code == 200
    assert patch(other.id, {'missed_ad_examples': 'Do not save'}).status_code == 403
    assert repo.get_by_id(other.id).missed_ad_examples == ''
    for payload in ({'language': 'en'}, {'missed_ad_examples': None}):
        assert patch(sub.id, payload).status_code == 200
        assert repo.get_by_id(sub.id).missed_ad_examples == 'Our sponsor offers a discount.'
    assert patch(sub.id, {'missed_ad_examples': 'x' * 20001}).status_code == 422
    assert patch(sub.id, {'missed_ad_examples': ''}).status_code == 200
    assert repo.get_by_id(sub.id).missed_ad_examples == ''
