from __future__ import absolute_import, division, print_function, unicode_literals

import json
from time import sleep

from postgres.orm import Model

from liberapay.cron import logger
from liberapay.models.participant import Participant
from liberapay.utils import utcnow
from liberapay.website import website


class Repository(Model):

    typname = "repositories"

    @property
    def url(self):
        platform = getattr(website.platforms, self.platform)
        return platform.repo_url.format(**self.__dict__)

    def get_owner(self):
        return self.db.one("""
            SELECT elsewhere.*::elsewhere_with_participant
              FROM elsewhere
             WHERE platform = %s
               AND domain = %s
               AND user_id = %s
        """, (self.platform, '', str(self.owner_id)))


def upsert_repos(cursor, repos, participant, info_fetched_at):
    if not repos:
        return repos
    r = []
    for repo in repos:
        if not repo.owner_id or not repo.last_update:
            continue
        repo.participant = participant.id
        repo.extra_info = json.dumps(repo.extra_info)
        repo.info_fetched_at = info_fetched_at
        cols, vals = zip(*repo.__dict__.items())
        on_conflict_set = ','.join('{0}=excluded.{0}'.format(col) for col in cols)
        cols = ','.join(cols)
        placeholders = ('%s,'*len(vals))[:-1]
        cursor.run("""
            DELETE FROM repositories
             WHERE platform = %s
               AND slug = %s
               AND remote_id <> %s
        """, (repo.platform, repo.slug, repo.remote_id))
        r.append(cursor.one("""
            INSERT INTO repositories
                        ({0})
                 VALUES ({1})
            ON CONFLICT (platform, remote_id) DO UPDATE
                    SET {2}
              RETURNING repositories
        """.format(cols, placeholders, on_conflict_set), vals))
    return r


def refetch_repos():
    with website.db.get_cursor() as cursor:
        repo = cursor.one("""
            SELECT r.participant, r.platform
              FROM repositories r
             WHERE r.info_fetched_at < now() - interval '6 days'
               AND r.participant IS NOT NULL
          ORDER BY r.info_fetched_at ASC
             LIMIT 1
        """)
        if not repo:
            return
        participant = Participant.from_id(repo.participant)
        account = participant.get_account_elsewhere(repo.platform)
        sess = account.get_auth_session()
        start_time = utcnow()
        logger.debug(
            "Refetching repository data for participant ~%s from %s account %s" %
            (participant.id, account.platform, account.user_id)
        )
        next_page = None
        for i in range(10):
            r = account.platform_data.get_repos(account, page_url=next_page, sess=sess)
            upsert_repos(cursor, r[0], participant, utcnow())
            next_page = r[2].get('next')
            if not next_page:
                break
            sleep(1)
        deleted_count = cursor.one("""
            WITH deleted AS (
                     DELETE FROM repositories
                      WHERE participant = %s
                        AND platform = %s
                        AND info_fetched_at < %s
                  RETURNING id
                 )
            SELECT count(*) FROM deleted
        """, (participant.id, account.platform, start_time))
        event_type = 'fetch_repos:%s' % account.id
        payload = dict(partial_list=bool(next_page), deleted_count=deleted_count)
        participant.add_event(cursor, event_type, payload)
