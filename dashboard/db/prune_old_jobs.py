"""Inventory, back up, then prune dated old jobs. Never delete hiring posts."""
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import IsolationLevel
from psycopg.conninfo import conninfo_to_dict


def connections():
    result, seen = {}, set()
    for key in ('NEON_DATABASE_URL', 'JOBS_DATABASE_URL_2', 'JOBS_DATABASE_URL_3', 'JOBS_DATABASE_URL_4'):
        url = os.getenv(key)
        if not url:
            continue
        info = conninfo_to_dict(url)
        identity = hashlib.sha256(json.dumps([info.get('host', '').replace('-pooler', ''),
                                              info.get('port', '5432'), info.get('dbname')]).encode()).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        result[key] = (url, identity)
    if 'NEON_DATABASE_URL' not in result:
        raise RuntimeError('Main jobs connection is required to check saved/applied protection')
    return result


def connect(url, readonly=True):
    conn = psycopg.connect(url, connect_timeout=30)
    conn.read_only = readonly
    conn.execute("SET statement_timeout = '15min'")
    conn.execute("SET lock_timeout = '5s'")
    return conn


def protected(main_url):
    with connect(main_url) as conn:
        if conn.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
            return []
        return [str(r[0]) for r in conn.execute("SELECT DISTINCT job_id FROM public.user_job_recommendations "
                                               "WHERE status IN ('saved', 'applied')")]


def guard(conn):
    if conn.execute("SELECT to_regclass('public.jobs')").fetchone()[0] is None:
        return False
    columns = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_schema='public' AND table_name='jobs'")}
    if not {'id', 'posted_at'}.issubset(columns):
        raise RuntimeError('Jobs table lacks id or posted_at; refusing an age guess')
    # Refuse even indirect deletes into the posts table through custom triggers or FKs.
    hooks = conn.execute("SELECT p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                         "WHERE t.tgrelid='public.jobs'::regclass AND NOT t.tgisinternal "
                         "AND (t.tgtype & 8) <> 0").fetchall()
    if any('linkedin_posts' in str(r[0]).lower() for r in hooks):
        raise RuntimeError('Jobs DELETE trigger references hiring posts; manual review required')
    fk = conn.execute("SELECT 1 FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid "
                       "WHERE c.contype='f' AND c.confrelid='public.jobs'::regclass "
                       "AND child.relname='linkedin_posts'").fetchone()
    if fk:
        raise RuntimeError('Hiring posts reference jobs; refusing possible cascading changes')
    return True


WHERE = 'posted_at < %s AND NOT (id::text = ANY(%s::text[]))'


def prepare(args, dbs, cutoff, saved):
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {'cutoff_utc': cutoff.isoformat(), 'older_than_days': args.days,
                'saved_or_applied_ids_protected': len(saved), 'databases': [],
                'policy': 'Only public.jobs with known posted_at before cutoff; all job boards; hiring posts untouched.'}
    for alias, (url, identity) in dbs.items():
        with psycopg.connect(url, connect_timeout=30) as conn:
            conn.read_only = True
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.execute("SET statement_timeout = '15min'")
            if not guard(conn):
                manifest['databases'].append({'alias': alias, 'jobs_table': False})
                continue
            total, unknown, old = conn.execute("SELECT count(*), count(*) FILTER (WHERE posted_at IS NULL), "
                                               "count(*) FILTER (WHERE posted_at < %s) FROM public.jobs", (cutoff,)).fetchone()
            eligible = conn.execute('SELECT count(*) FROM public.jobs WHERE ' + WHERE, (cutoff, saved)).fetchone()[0]
            item = {'alias': alias, 'identity': identity, 'jobs_table': True, 'total_jobs': total,
                    'unknown_dates_preserved': unknown, 'older_than_cutoff': old, 'eligible': eligible,
                    'saved_or_applied_old_preserved': old - eligible}
            board_counts = conn.execute('SELECT source_board, count(*) FROM public.jobs WHERE ' + WHERE
                                         + ' GROUP BY source_board ORDER BY count(*) DESC', (cutoff, saved)).fetchall()
            item['eligible_by_board'] = {str(board): n for board, n in board_counts}
            if args.mode == 'backup':
                ids = [str(r[0]) for r in conn.execute('SELECT id FROM public.jobs WHERE ' + WHERE, (cutoff, saved))]
                if len(ids) != eligible:
                    raise RuntimeError('Snapshot candidate count changed')
                csv_path = args.output / (alias + '.csv.gz')
                with gzip.open(csv_path, 'wb', compresslevel=6) as f, conn.cursor() as cur:
                    with cur.copy('COPY (SELECT * FROM public.jobs WHERE ' + WHERE + ') TO STDOUT WITH CSV HEADER',
                                  (cutoff, saved)) as copy:
                        for block in copy:
                            f.write(block)
                ids_path = args.output / (alias + '.ids.json.gz')
                with gzip.open(ids_path, 'wt') as f:
                    json.dump(ids, f)
                item.update(csv_file=csv_path.name, ids_file=ids_path.name,
                            csv_sha256=hashlib.file_digest(csv_path.open('rb'), 'sha256').hexdigest())
            manifest['databases'].append(item)
            print(json.dumps(item), flush=True)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def delete(args, dbs):
    manifest = json.loads((args.output / 'manifest.json').read_text())
    cutoff = datetime.fromisoformat(manifest['cutoff_utc'])
    # The action is intentionally limited to the user-authorized 30-day cleanup.
    if manifest['older_than_days'] != 30 or cutoff > datetime.now(timezone.utc) - timedelta(days=30):
        raise RuntimeError('Refusing any cutoff newer than the authorized 30-day window')
    result = []
    for item in manifest['databases']:
        if not item.get('jobs_table'):
            continue
        alias = item['alias']
        url, identity = dbs[alias]
        if identity != item['identity']:
            raise RuntimeError('Database identity differs from the backed-up snapshot')
        path = args.output / item['csv_file']
        with path.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() != item['csv_sha256']:
                raise RuntimeError('Backup integrity mismatch')
        with gzip.open(args.output / item['ids_file'], 'rt') as f:
            ids = json.load(f)
        if len(ids) != item['eligible'] or len(ids) != len(set(ids)):
            raise RuntimeError('Backup ID manifest is inconsistent')
        deleted = 0
        with connect(url, readonly=False) as conn:
            guard(conn)
            for offset in range(0, len(ids), 1000):
                saved = protected(dbs['NEON_DATABASE_URL'][0])
                cur = conn.execute('DELETE FROM public.jobs WHERE id::text = ANY(%s::text[]) AND ' + WHERE,
                                   (ids[offset:offset+1000], cutoff, saved))
                deleted += cur.rowcount
                conn.commit()
            remaining = conn.execute('SELECT count(*) FROM public.jobs').fetchone()[0]
        # Reuse freed pages for incoming workflow jobs; no full-table rewrite/lock.
        if deleted:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute("SET statement_timeout = '15min'")
                conn.execute('VACUUM (ANALYZE) public.jobs')
        result.append({'alias': alias, 'deleted': deleted, 'remaining': remaining})
        print(json.dumps(result[-1]), flush=True)
    report = {'cutoff_utc': manifest['cutoff_utc'], 'tables_deleted_from': ['public.jobs'],
              'hiring_posts_modified': False, 'databases': result}
    (args.output / 'deletion-report.json').write_text(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('inventory', 'backup', 'delete'))
    parser.add_argument('--days', type=int, choices=(30,), default=30)
    parser.add_argument('--output', type=Path, default=Path('neon-job-cleanup'))
    args = parser.parse_args()
    dbs = connections()
    if args.mode == 'delete':
        delete(args, dbs)
    else:
        prepare(args, dbs, datetime.now(timezone.utc) - timedelta(days=args.days),
                protected(dbs['NEON_DATABASE_URL'][0]))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Connection errors may include connection details; do not emit them.
        print('Cleanup stopped safely:', type(exc).__name__, flush=True)
        raise SystemExit(1)
