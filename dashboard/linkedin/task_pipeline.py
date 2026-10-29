"""Bounded, resumable role/region tasks. Planning requires only Python's stdlib."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

from role_selection import select_roles

HERE = Path(__file__).resolve().parent


def literal(path, variable):
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            if isinstance(target, ast.Name) and target.id == variable:
                return ast.literal_eval(node.value)
    raise ValueError(f'{variable} not found')


def task_id(role, phase, group=0):
    digest = hashlib.sha256(role.strip().casefold().encode()).hexdigest()[:16]
    return f'{phase}-{digest}-{group}'


def plan(roles, phase, mode='auto', manifests=(), groups=5):
    states = literal(HERE/'linkedin_guest_search.py', 'US_STATES')
    summaries = {m['role'].casefold(): m for m in manifests if m.get('kind') == 'national'}
    tasks = []
    for role in roles:
        if phase == 'national':
            tasks.append({'id': task_id(role, phase), 'role': role, 'kind': phase, 'locations': ['United States']})
            continue
        previous = summaries.get(role.casefold())
        # A missing/failed national task must never prevent geographic expansion.
        if mode == 'auto' and previous and previous.get('complete') and not previous.get('capped'):
            continue
        for group in range(groups):
            selected = select_roles(states, shard=f'{group+1}/{groups}')
            tasks.append({'id': task_id(role, phase, group), 'role': role, 'kind': phase,
                          'locations': [f'{state}, United States' for state in selected]})
    if len(tasks) > 256:
        raise ValueError('Task matrix exceeds 256; reduce the selected role range')
    return tasks


def posting_key(job):
    url = str(job.get('job_url') or job.get('url') or '')
    match = re.search(r'/jobs/view/(?:[^/?#]*-)?(\d+)', url)
    if match:
        return 'linkedin-' + match[1]
    return str(job.get('id') or url or hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest())


def merge_record(previous, incoming):
    """Keep the best details when a later listing-only checkpoint repeats a job."""
    value = dict(previous or {})
    for key, new in incoming.items():
        if new is None or new == '' or new == [] or new == {}:
            continue
        fetched = str(incoming.get('_linkedin_detail_fetched', '')).casefold() in {'true', '1'}
        if key == 'description' and not fetched and len(str(new)) < len(str(value.get(key) or '')):
            continue
        value[key] = new
    return value


def read_records(path):
    """A torn last journal line cannot discard all earlier durable records."""
    path = Path(path)
    if path.suffix == '.jsonl':
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    print(f'::warning::Ignoring a torn journal record in {path.name}; cursor rewind will retry it.')
                    continue
                if isinstance(value, dict): yield value
    else:
        csv.field_size_limit(2**31-1)
        with path.open(encoding='utf-8-sig', errors='replace', newline='') as handle:
            yield from csv.DictReader(handle)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class TaskStore:
    def __init__(self, root, task, post_time, resume=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.stem = f'linkedin_shard_{task["id"]}'
        self.journal = self.root/f'{self.stem}_jobs.jsonl'
        self.state_path = self.root/f'{self.stem}_state.json'
        self.csv_path = self.root/f'{self.stem}_jobs.csv'
        self.jobs = {}
        self.state = {'role': task['role'], 'kind': task['kind'], 'task_id': task['id'],
                      'post_time': post_time, 'locations': {}, 'complete': False, 'capped': False,
                      'expected_locations': list(task['locations'])}
        sources = [self.root]
        if resume and Path(resume).exists(): sources.insert(0, Path(resume))
        for source in sources:
            for path in sorted(source.rglob(f'{self.stem}_state.json')):
                saved = json.loads(path.read_text())
                if saved.get('task_id') == task['id'] and saved.get('post_time') != post_time:
                    raise ValueError('Resume requires the same post_time as the saved checkpoint')
                if saved.get('task_id') == task['id']:
                    self.state = saved
            for pattern in [f'{self.stem}_jobs.csv', f'{self.stem}_jobs.jsonl']:
                for path in sorted(source.rglob(pattern)):
                    for record in read_records(path):
                        key = posting_key(record)
                        self.jobs[key] = merge_record(self.jobs.get(key), record)
        # Copy restored data locally before any network request. A subsequent
        # resume only needs this run's artifacts, not the entire ancestor chain.
        if self.jobs:
            with self.journal.open('w', encoding='utf-8') as handle:
                for record in self.jobs.values(): handle.write(json.dumps(record, ensure_ascii=False)+'\n')
                handle.flush(); os.fsync(handle.fileno())
        self.state['expected_locations'] = list(task['locations'])
        self.save()

    def checkpoint(self, location, records, progress):
        # Data must reach disk before its cursor advances.
        with self.journal.open('a', encoding='utf-8') as handle:
            for record in records:
                key = posting_key(record)
                record = dict(record)
                if record.get('id') != key:
                    record['legacy_id'] = record.get('id')
                record['id'] = key
                record['search_role'] = self.task['role']
                record['search_location'] = location
                self.jobs[key] = merge_record(self.jobs.get(key), record)
                handle.write(json.dumps(self.jobs[key], ensure_ascii=False, default=str)+'\n')
            handle.flush(); os.fsync(handle.fileno())
        previous = self.state['locations'].get(location, {})
        self.state['locations'][location] = {**previous, **progress}
        self.save()

    def save(self):
        locations = self.state['locations']
        self.state['complete'] = all(locations.get(name, {}).get('done') for name in self.task['locations'])
        self.state['capped'] = any(part.get('capped') for part in locations.values())
        self.state['saved_jobs'] = len(self.jobs)
        self.state['missing_descriptions'] = sum(not job.get('description') for job in self.jobs.values())
        atomic_json(self.state_path, self.state)

    def snapshot(self):
        self.save()
        if not self.jobs: return
        temporary = self.csv_path.with_suffix('.csv.tmp')
        fields = list(dict.fromkeys(k for job in self.jobs.values() for k in job))
        with temporary.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for job in self.jobs.values():
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                 for k, v in job.items()})
            handle.flush(); os.fsync(handle.fileno())
        temporary.replace(self.csv_path)
        # Compact raw/detail updates without sacrificing the last durable file.
        journal_tmp = self.journal.with_suffix('.jsonl.tmp')
        with journal_tmp.open('w', encoding='utf-8') as handle:
            for record in self.jobs.values():
                handle.write(json.dumps(record, ensure_ascii=False, default=str)+'\n')
            handle.flush(); os.fsync(handle.fileno())
        journal_tmp.replace(self.journal)


def crawl(task, post_time, output, resume=None, minutes=330):
    import linkedin_68_roles as scraper
    store = TaskStore(output, task, post_time, resume)
    scraper._deadline = time.monotonic() + max(0, minutes*60-90)
    seen = {key for key, job in store.jobs.items() if job.get('description')}
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        for location in task['locations']:
            previous = store.state['locations'].get(location, {})
            if previous.get('done'): continue
            if scraper.out_of_time(): break
            # A small rewind accommodates shifted results and interrupted details.
            offset = max(0, int(previous.get('next_offset', 0))-scraper.BATCH_SIZE)
            try:
                jobs, stats = scraper.scrape_role(task['role'], location, scraper.POST_TIME_MAP.get(post_time),
                    seen, True, start_offset=offset, on_page=lambda rows, progress: store.checkpoint(location, rows, progress),
                    stop_on_irrelevant=False)
                done = bool(stats.get('exhausted') or stats.get('capped')) and not stats.get('blocked')
                store.checkpoint(location, jobs, {'next_offset': stats.get('next_offset', offset),
                    'done': done, 'capped': bool(stats.get('capped')), 'reason': stats.get('stop_reason', 'unknown')})
            except Exception as exc:
                print(f'::warning::{task["role"]} / {location} paused: {type(exc).__name__}; saved journal retained.')
                store.checkpoint(location, [], {'done': False, 'reason': type(exc).__name__})
            store.snapshot()
        # Detail failures remain retryable, even if all listing pages finished.
        missing = [job for job in store.jobs.values() if not job.get('description')]
        for job in missing[:40]:
            if scraper.out_of_time(45): break
            try:
                detail = scraper._fetch_one_job_details(job['job_url'])
                updated = dict(job)
                scraper._merge_job_details(updated, detail)
                store.checkpoint(job.get('search_location') or task['locations'][0], [updated], {})
            except Exception:
                pass  # The row stays saved; missing_descriptions exposes the retry backlog.
    except KeyboardInterrupt:
        print('::warning::Interrupted; durable rows and unfinished cursors retained.')
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        store.snapshot()
    pending = [name for name in task['locations'] if not store.state['locations'].get(name, {}).get('done')]
    capped = [name for name, part in store.state['locations'].items() if part.get('capped')]
    missing = store.state.get('missing_descriptions', 0)
    summary = f'{task["role"]}: {len(store.jobs)} unique postings saved; {len(pending)} regions pending; {len(capped)} capped; {missing} JDs pending.'
    print(summary)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write(f'### {task["role"]}\n\n{summary}\n\n')
            if pending: handle.write('Pending: '+', '.join(pending)+'\n\n')
            if capped: handle.write('Capped (needs narrower searches): '+', '.join(capped)+'\n\n')
    return 3 if pending or capped or missing else 0


def report(root, destination, expected=(), jobs_csv=None):
    states = {}
    for path in sorted(Path(root).rglob('*_state.json'), key=lambda p: ('previous' not in p.parts, str(p))):
        value = json.loads(path.read_text())
        if value.get('task_id'): states[value['task_id']] = value
    for task in expected:
        states.setdefault(task['id'], {'task_id': task['id'], 'role': task['role'], 'kind': task['kind'],
            'expected_locations': task['locations'], 'locations': {}, 'missing_task_artifact': True})
    pending, capped, missing_details = [], [], 0
    for value in states.values():
        missing_details += value.get('missing_descriptions', 0)
        for location in value.get('expected_locations', []):
            status = value.get('locations', {}).get(location, {})
            record = {'task_id': value['task_id'], 'role': value['role'], 'location': location,
                      'next_offset': status.get('next_offset', 0), 'reason': status.get('reason', 'not run')}
            if not status.get('done'): pending.append(record)
            if status.get('capped'): capped.append(record)
    unique_missing = None
    unique_total = 0
    if jobs_csv and Path(jobs_csv).exists():
        unique_missing = 0
        for row in read_records(jobs_csv):
            unique_total += 1
            unique_missing += not row.get('description')
    pipeline_error = any(os.getenv(key) not in (None, '', 'success') for key in ['STATE_PLAN_RESULT', 'MERGE_RESULT'])
    value = {'pending_regions': pending, 'capped_regions': capped,
             'missing_description_records_before_cross_task_dedup': missing_details,
             'unique_postings_missing_descriptions': unique_missing,
             'unique_postings_saved': unique_total,
             'pipeline_error': pipeline_error,
             'complete': not pending and not capped and not pipeline_error and not (unique_missing if unique_missing is not None else missing_details),
             'resume_run_id': os.getenv('GITHUB_RUN_ID'), 'tasks': list(states.values())}
    atomic_json(Path(destination), value)
    summary = (f'{unique_total} unique postings retained; {len(states)} tasks; {len(pending)} regions pending; {len(capped)} searches still capped. '
               'Collected rows are retained in the raw CSV and task journals.')
    if unique_missing is not None:
        summary += f' {unique_missing} unique postings still need descriptions.'
    if pipeline_error:
        summary += ' Planning or export failed; coverage is not confirmed.'
    print(summary)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write('### Crawl progress: '+('COMPLETE' if value['complete'] else 'PARTIAL')+'\n\n'+summary+'\n\n')
            handle.write(f'Continue unfinished work with `resume_run_id: {os.getenv("GITHUB_RUN_ID", "this run ID")}` and the same roles/window.\n\n')
            if capped: handle.write('Capped searches need narrower segmentation; rerunning the same offset cannot exceed LinkedIn’s limit.\n\n')
    return value


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    planning = sub.add_parser('plan')
    planning.add_argument('--phase', choices=['national', 'states'], required=True)
    planning.add_argument('--role-start', type=int, default=1)
    planning.add_argument('--role-end', type=int, default=23)
    planning.add_argument('--roles', default='')
    planning.add_argument('--search-terms', default='')
    planning.add_argument('--state-mode', choices=['auto', 'all'], default='auto')
    planning.add_argument('--previous', default='')
    running = sub.add_parser('crawl')
    running.add_argument('--task-json', required=True)
    running.add_argument('--post-time', default='week')
    running.add_argument('--output', required=True)
    running.add_argument('--resume')
    running.add_argument('--minutes', type=float, default=330)
    reporting = sub.add_parser('report')
    reporting.add_argument('--root', required=True)
    reporting.add_argument('--output', required=True)
    reporting.add_argument('--jobs-csv')
    args = parser.parse_args()
    if args.command == 'crawl':
        raise SystemExit(crawl(json.loads(args.task_json), args.post_time, args.output, args.resume, args.minutes))
    if args.command == 'report':
        expected = []
        for key in ['NATIONAL_PLAN', 'STATE_PLAN']:
            expected += json.loads(os.getenv(key) or '{}').get('include', [])
        report(args.root, args.output, [t for t in expected if t.get('role')], args.jobs_csv)
        return
    catalog = literal(HERE/'linkedin_68_roles.py', 'ROLES')
    if args.search_terms:
        selected = list({t.strip().casefold(): t.strip() for t in args.search_terms.split(',') if t.strip()}.values())
    else:
        selected = select_roles(catalog, filters=args.roles.split(',') if args.roles.strip() else None,
                                start_idx=args.role_start, end_idx=args.role_end)
    manifests = []
    if args.previous and Path(args.previous).exists():
        for path in Path(args.previous).rglob('*_state.json'):
            manifests.append(json.loads(path.read_text()))
    tasks = plan(selected, args.phase, args.state_mode, manifests)
    if not selected: raise ValueError('No roles selected')
    matrix = json.dumps({'include': tasks or [{'id': 'none', 'role': '', 'kind': args.phase, 'locations': []}]})
    print(f'{len(selected)} selected roles, {len(tasks)} {args.phase} task(s)')
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
            handle.write(f'matrix={matrix}\nhas_tasks={str(bool(tasks)).lower()}\n')
    else: print(matrix)


if __name__ == '__main__': main()
