#!/usr/bin/env python3
"""One queue for every Codex job of every session, so that they stop fighting over the connection.

A single dispatcher runs at most `--slots` jobs at once (4 by default) and always starts the waiting job with the
lowest priority number first (1 urgent, 5 normal, 9 background), the oldest first among equals. The queue lives in
~/.local/state/codex-queue/ ($CODEX_QUEUE_DIR to change it), shared by every project and session.

Each job records where it comes from: its project (the main git repository), its worktree and branch, taken from
--origin (the current directory by default), and its caller (--caller, else $CODEX_QUEUE_CALLER, else the Claude Code
session id). It belongs to a session: --session, else $CODEX_QUEUE_SESSION, else the branch.

  codex-queue add --priority 5 --label <what> [--workdir <dir>] --prompt-file <file> [--add-dir <dir>]...
                  [--origin <project dir>] [--session <s>] [--caller <id>] [-- <codex exec options>]
  codex-queue wait <id>                       blocks until the job ends, prints its state and log
  codex-queue list [--session <s>] [--since 1h | --all]   jobs by session: running, waiting and held; --since adds
                                              those finished within that time (with their end), --all every kept one
  codex-queue keep [6h]                       how long finished jobs stay listed (read live)
  codex-queue priority <1-9> <id>...         new priority of waiting or held jobs, chosen by id,
  codex-queue priority <1-9> --session <s>   by session,
  codex-queue priority <1-9> --label <p>     or by label prefix (the selectors combine)
  codex-queue hold | release <selector>      keep jobs out of the dispatcher, or give them back
  codex-queue cancel <selector>              drop waiting jobs, stop running ones
  codex-queue slots [N]                       how many codex exec run at once (4 by default), read live
  codex-queue pause | resume                  stop launching waiting jobs (running ones finish), or start again
  codex-queue fair [on|off]                   take turns between sessions at each free slot (priority still first)
  codex-queue interleave                      rewrite the queue order once so that sessions alternate
  codex-queue retry-missing                   queue again the finished jobs whose expected files are missing
  codex-queue stop-all                        pause the queue and stop every running job
  codex-queue dispatch                        the dispatcher itself (`add` starts it when it is not running)
  codex-queue ui [--port 8765]                web page on localhost: drag jobs to reorder them, hold, cancel, and see
                                              the images each Codex session generated
"""
import argparse
import fcntl
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(os.environ.get('CODEX_QUEUE_DIR', Path.home() / '.local/state/codex-queue'))
JOBS = ROOT / 'jobs'
LOGS = ROOT / 'logs'
LOCK = ROOT / 'dispatcher.lock'
WORK = ROOT / 'work'
DEFAULT_SLOTS = 4
SETTINGS = ROOT / 'settings.json'
POLL_SECONDS = 5
DEFAULT_KEEP = '6h'
DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
STATES = ('running', 'waiting', 'held', 'done', 'failed', 'cancelled')
OPEN_STATES = ('running', 'waiting', 'held')
CODEX_IMAGES = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'generated_images'
UI_PAGE = Path(__file__).resolve().with_name('codex_queue_ui.html')
SAFE_NAME = re.compile(r'^[\w.-]+$')
DEFAULT_PORT = 8765
LOG_TAIL_CHARACTERS = 20000


def read_job(path: Path) -> dict:
    return json.loads(path.read_text())


def write_job(job: dict) -> None:
    path = JOBS / f"{job['id']}.json"
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, indent=1))
    temporary.replace(path)


def queue_key(job: dict) -> tuple:
    """Dispatch order: priority first, then `order` (set when a job is dragged in the page), else age."""
    return job['priority'], job.get('order', job['created'])


def all_jobs() -> list:
    jobs = []
    for path in JOBS.glob('*.json'):
        try:
            jobs.append(read_job(path))
        except (OSError, json.JSONDecodeError):
            continue
    return jobs


def seconds_of(duration: str) -> int:
    """'90s', '30m', '6h', '2d' in seconds; a bare number is minutes."""
    text = duration.strip().lower()
    if text and text[-1] in DURATION_UNITS:
        return int(float(text[:-1]) * DURATION_UNITS[text[-1]])
    return int(float(text) * 60)


def duration_argument(text: str) -> str:
    try:
        seconds_of(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a duration: {text} (examples: 30m, 6h, 2d)')
    return text


def settings() -> dict:
    """Live settings: the dispatcher reads them again at every turn."""
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({**settings(), key: value}))


def slot_count() -> int:
    """How many codex exec may run at once."""
    try:
        return int(settings()['slots'])
    except (KeyError, ValueError):
        return DEFAULT_SLOTS


def kept_seconds() -> int:
    """How long a finished job stays in the queue before it is forgotten."""
    try:
        return seconds_of(str(settings().get('keep', DEFAULT_KEEP)))
    except ValueError:
        return seconds_of(DEFAULT_KEEP)


def is_fair() -> bool:
    """In fair mode, sessions take turns: a free slot goes to the session with the fewest jobs running."""
    return settings().get('fair') is True


def launch_order(candidates: list, running_by_session: dict, is_taking_turns: bool) -> list:
    """The order in which the dispatcher would start these jobs; taking turns, priority still comes first."""
    ordered = sorted(candidates, key=queue_key)
    if not is_taking_turns:
        return ordered
    served = dict(running_by_session)
    launched = []
    while ordered:
        chosen = min(ordered, key=lambda job: (job['priority'], served.get(job.get('session'), 0), queue_key(job)))
        ordered.remove(chosen)
        served[chosen.get('session')] = served.get(chosen.get('session'), 0) + 1
        launched.append(chosen)
    return launched


def running_by_session(jobs: list) -> dict:
    counts: dict = {}
    for job in jobs:
        if job['state'] == 'running':
            counts[job.get('session')] = counts.get(job.get('session'), 0) + 1
    return counts


INTERLEAVE_STEP_SECONDS = 0.001


def interleave_queue() -> int:
    """Gives waiting and held jobs an `order` alternating between sessions; jobs queued later come after them."""
    queued = [job for job in all_jobs() if job['state'] in ('waiting', 'held')]
    if not queued:
        return 0
    first_key = min(queue_key(job)[1] for job in queued)
    for index, job in enumerate(launch_order(queued, {}, True)):
        job['order'] = first_key + index * INTERLEAVE_STEP_SECONDS
        write_job(job)
    return len(queued)


def interleave(arguments) -> None:
    print(f'{interleave_queue()} jobs reordered, sessions alternate')


def set_fair(arguments) -> None:
    if arguments.state is not None:
        save_setting('fair', arguments.state == 'on')
    print('sessions take turns' if is_fair() else 'queue order')


def is_paused() -> bool:
    """While paused, the dispatcher launches nothing; running jobs go on to their end."""
    return settings().get('paused') is True


def set_paused(is_pausing: bool) -> None:
    save_setting('paused', is_pausing)
    if not is_pausing:
        start_dispatcher()


def pause(arguments) -> None:
    set_paused(True)
    print('queue paused: no new job starts, running ones finish')


def resume(arguments) -> None:
    set_paused(False)
    print('queue resumed')


def set_slots(arguments) -> None:
    if arguments.count is not None:
        save_setting('slots', arguments.count)
    print(f'{slot_count()} codex exec at once')


def set_keep(arguments) -> None:
    if arguments.duration is not None:
        save_setting('keep', arguments.duration)
    print(f"finished jobs kept {settings().get('keep', DEFAULT_KEEP)}")


def is_dispatcher_running() -> bool:
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(LOCK, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def start_dispatcher() -> None:
    if is_dispatcher_running():
        return
    subprocess.Popen([sys.executable, __file__, 'dispatch'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(ROOT / 'dispatcher.log', 'a'), start_new_session=True)


def git(directory: Path, *arguments: str) -> str:
    answer = subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, text=True)
    return answer.stdout.strip() if answer.returncode == 0 else ''


def origin_of(directory: Path) -> dict:
    """Project (the main repository, shared by its worktrees), worktree and branch of the directory a job comes from."""
    common = git(directory, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    top = git(directory, 'rev-parse', '--show-toplevel')
    root = Path(common).parent if common else directory
    inside = Path(top) if top else directory
    # A project kept in a sub-folder of its repository (monorepo/app) is named with that folder.
    relative = directory.relative_to(inside) if directory.is_relative_to(inside) else Path('.')
    folder = relative.parts[0] if relative.parts else ''
    return {
        'project': f'{root.name}/{folder}' if folder else root.name,
        'worktree': Path(top).name if top else directory.name,
        'branch': git(directory, 'rev-parse', '--abbrev-ref', 'HEAD'),
    }


def add(arguments) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    prompt = Path(arguments.prompt_file).read_text()
    origin = origin_of(Path(arguments.origin).resolve())
    job_id = uuid.uuid4().hex[:8]
    # The queue owns the default workdir: a caller's temporary folder may vanish while the job runs.
    workdir = Path(arguments.workdir).resolve() if arguments.workdir else WORK / job_id
    job = {
        'id': job_id,
        'label': arguments.label,
        'session': arguments.session or os.environ.get('CODEX_QUEUE_SESSION') or origin['branch'] or origin['worktree'],
        'caller': arguments.caller or os.environ.get('CODEX_QUEUE_CALLER') or os.environ.get('CLAUDE_CODE_SESSION_ID', ''),
        **origin,
        'priority': arguments.priority,
        'created': time.time(),
        'state': 'waiting',
        'workdir': str(workdir),
        'add_dirs': [str(Path(directory).resolve()) for directory in arguments.add_dir],
        'prompt': prompt,
        'codex_options': arguments.codex_options,
        'skip_if_exists': arguments.skip_if_exists,
    }
    write_job(job)
    print(job['id'])
    start_dispatcher()


def matching(arguments) -> list:
    """Jobs chosen by id, session and label prefix together; nothing when no selector is given."""
    if not (arguments.ids or arguments.session or arguments.label):
        sys.exit('choose jobs by id, --session or --label')
    return [
        job for job in all_jobs()
        if (not arguments.ids or job['id'] in arguments.ids)
        and (not arguments.session or job.get('session') == arguments.session)
        and (not arguments.label or job['label'].startswith(arguments.label))
    ]


def reprioritize_job(job: dict, priority: int, order: float | None = None) -> bool:
    if job['state'] not in ('waiting', 'held'):
        return False
    job['priority'] = priority
    if order is not None:
        job['order'] = order
    write_job(job)
    return True


def hold_job(job: dict) -> bool:
    if job['state'] != 'waiting':
        return False
    job['state'] = 'held'
    write_job(job)
    return True


def release_job(job: dict) -> bool:
    if job['state'] != 'held':
        return False
    job['state'] = 'waiting'
    write_job(job)
    return True


def cancel_job(job: dict) -> bool:
    if job['state'] == 'running' and job.get('pid'):
        try:
            os.killpg(job['pid'], 15)
        except ProcessLookupError:
            pass
    elif job['state'] not in ('waiting', 'held'):
        return False
    job.update(state='cancelled', ended=time.time())
    write_job(job)
    return True


def stop_all_running() -> list:
    """Pauses the queue, else the dispatcher would fill the freed slots at once, then stops every running job."""
    set_paused(True)
    stopped = [job for job in all_jobs() if job['state'] == 'running']
    for job in stopped:
        cancel_job(job)
    return stopped


def stop_all(arguments) -> None:
    for job in stop_all_running():
        print(job['id'], job['label'], 'stopped')
    print('queue paused')


def set_priority(arguments) -> None:
    for job in matching(arguments):
        if reprioritize_job(job, arguments.priority):
            print(job['id'], job['label'], '->', arguments.priority)


def hold(arguments) -> None:
    for job in matching(arguments):
        if hold_job(job):
            print(job['id'], job['label'], 'held')


def release(arguments) -> None:
    for job in matching(arguments):
        if release_job(job):
            print(job['id'], job['label'], 'waiting')
    start_dispatcher()


def cancel(arguments) -> None:
    for job in matching(arguments):
        cancel_job(job)
        print(job['id'], job['label'], job['state'])


def is_listed(job: dict, arguments) -> bool:
    if job['state'] in OPEN_STATES or arguments.all:
        return True
    return arguments.since is not None and time.time() - job.get('ended', job['created']) <= seconds_of(arguments.since)


def clock(moment: float) -> str:
    return time.strftime('%d/%m %H:%M', time.localtime(moment))


def show(arguments) -> None:
    jobs = [job for job in all_jobs() if is_listed(job, arguments)]
    if arguments.session:
        jobs = [job for job in jobs if job.get('session') == arguments.session]
    sessions = sorted({job.get('session', '?') for job in jobs})
    if is_paused():
        print('** queue paused: `codex-queue resume` to start waiting jobs again **')
    for session in sessions:
        own = sorted((job for job in jobs if job.get('session', '?') == session), key=lambda job: (STATES.index(job['state']), *queue_key(job)))
        counts = ', '.join(f"{sum(1 for job in own if job['state'] == state)} {state}" for state in STATES if any(job['state'] == state for job in own))
        first = own[0]
        where = f"{first.get('project', '?')} / {first.get('worktree', '?')}"
        callers = ', '.join(sorted({job.get('caller', '') for job in own if job.get('caller')})) or 'appelant inconnu'
        print(f"== {session}  [{where}]  {callers}  ({counts})")
        print(f"  {'state':9} {'p':2} {'id':8}  {'demandé':11}  {'début':11}  {'fin':11}  label")
        for job in own:
            times = [clock(job['created']), clock(job['started']) if 'started' in job else 'null', clock(job['ended']) if 'ended' in job else 'null']
            print(f"  {job['state']:9} p{job['priority']} {job['id']}  {times[0]:11}  {times[1]:11}  {times[2]:11}  {job['label']}")
    if not jobs:
        print('queue empty')


def wait(arguments) -> None:
    """Blocks until the job has finished, then prints its state: run it in the background to be told when it ends."""
    path = JOBS / f"{arguments.id}.json"
    while True:
        job = read_job(path)
        if job['state'] not in OPEN_STATES:
            print(job['id'], job['label'], job['state'], LOGS / f"{job['id']}.log")
            sys.exit(0 if job['state'] == 'done' else 1)
        time.sleep(POLL_SECONDS)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def outputs_exist(job: dict) -> bool:
    """A job naming its output files (skip_if_exists) is pointless once they all exist."""
    files = job.get('skip_if_exists') or []
    return bool(files) and all(Path(file).exists() for file in files)


def final_state(job: dict, exit_code: int) -> str:
    """A job that names its outputs succeeds only if they all exist: codex exec can exit 0 without painting
    (image quota reached, missing reference), and a job followed by its pid alone has no exit code at all."""
    has_outputs = bool(job.get('skip_if_exists'))
    if exit_code != 0 or (has_outputs and not outputs_exist(job)):
        return 'failed'
    return 'done'


def lacks_outputs(job: dict) -> bool:
    """A done or failed job that names output files and did not leave them all."""
    return job['state'] in ('done', 'failed') and bool(job.get('skip_if_exists')) and not outputs_exist(job)


def retry_missing_outputs() -> list:
    """Puts back in the queue, with their priority and place, the finished jobs whose expected files are missing."""
    retried = [job for job in all_jobs() if lacks_outputs(job)]
    for job in retried:
        for run_field in ('started', 'ended', 'pid'):
            job.pop(run_field, None)
        job['state'] = 'waiting'
        write_job(job)
    if retried:
        start_dispatcher()
    return retried


def retry_missing(arguments) -> None:
    for job in retry_missing_outputs():
        print(job['id'], job['label'], 'waiting')


def launch(job: dict) -> subprocess.Popen:
    command = ['codex', 'exec', '-C', job['workdir'], '--skip-git-repo-check']
    for directory in job['add_dirs']:
        command += ['--add-dir', directory]
    command += job.get('codex_options', [])
    command.append(job['prompt'])
    Path(job['workdir']).mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{job['id']}.log", 'w')
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    job.update(state='running', pid=process.pid, started=time.time())
    write_job(job)
    return process


def dispatch(arguments) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    JOBS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK, 'a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    children = {}
    while True:
        jobs = all_jobs()
        for job in jobs:
            if job['state'] != 'running':
                continue
            child = children.get(job['id'])
            # A job launched by an earlier dispatcher is followed by its pid alone.
            finished = child.poll() is not None if child else not is_alive(job.get('pid', 0))
            if finished:
                code = child.returncode if child else 0
                if child:
                    job['exit_code'] = code
                job.update(state=final_state(job, code), ended=time.time())
                write_job(job)
                children.pop(job['id'], None)
        for job in jobs:
            if job['state'] in ('done', 'failed', 'cancelled') and time.time() - job.get('ended', job['created']) > kept_seconds():
                (JOBS / f"{job['id']}.json").unlink(missing_ok=True)
                shutil.rmtree(WORK / job['id'], ignore_errors=True)
        running = sum(1 for job in jobs if job['state'] == 'running')
        waiting = launch_order([job for job in jobs if job['state'] == 'waiting'], running_by_session(jobs), is_fair())
        for job in waiting:
            if running >= slot_count() or is_paused():
                break
            if outputs_exist(job):
                job.update(state='done', ended=time.time())
                write_job(job)
                continue
            children[job['id']] = launch(job)
            running += 1
        if not waiting and running == 0 and not any(job['state'] == 'running' for job in all_jobs()):
            time.sleep(POLL_SECONDS * 12)
            if not any(job['state'] in ('waiting', 'running') for job in all_jobs()):
                return
        time.sleep(POLL_SECONDS)


def codex_session_of(job: dict) -> str:
    """The Codex session id printed at the top of the job's log: it names the job's generated_images folder."""
    try:
        with open(LOGS / f"{job['id']}.log", errors='replace') as log:
            head = log.read(8192)
    except OSError:
        return ''
    found = re.search(r'^session id: ([\w-]+)$', head, re.MULTILINE)
    return found.group(1) if found else ''


def generated_images(codex_session: str) -> list:
    folder = CODEX_IMAGES / codex_session
    if not codex_session or not folder.is_dir():
        return []
    return [path.name for path in sorted((path for path in folder.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime)]


# Trouble a running codex exec reports in its log, checked in this order on its last lines.
ALERT_PATTERNS = (
    ('quota', re.compile(r'usage_limit_reached|The usage limit has been reached|Too Many Requests')),
    ('disque plein', re.compile(r'No space left on device|StorageFull')),
    ('hors réseau', re.compile(r'waiting for network|failed to lookup address')),
    ('reconnexion', re.compile(r'Reconnecting|stream disconnected|failed to connect to websocket')),
    ('erreur', re.compile(r'\bERROR\b|\bError:')),
)
# Logged every few minutes by every healthy codex exec: not a sign of trouble.
HARMLESS_LOG_LINE = re.compile(r'failed to (refresh available models|renew cache TTL|write models cache)')
ALERT_LINES = 12


def running_alert(job: dict) -> dict | None:
    """The latest trouble among the last lines of a running job's log, or None while it looks healthy."""
    if job['state'] != 'running':
        return None
    last_lines = [line for line in log_tail(job['id']).splitlines() if line.strip()][-ALERT_LINES:]
    for line in reversed(last_lines):
        if HARMLESS_LOG_LINE.search(line):
            continue
        for kind, pattern in ALERT_PATTERNS:
            if pattern.search(line):
                return {'kind': kind, 'line': line.strip()[:300]}
    return None


def job_summary(job: dict) -> dict:
    codex_session = codex_session_of(job)
    summary = {key: job_value for key, job_value in job.items() if key != 'prompt'}
    return {**summary, 'codex_session': codex_session, 'images': generated_images(codex_session), 'alert': running_alert(job)}


def job_by_id(job_id: str) -> dict | None:
    if not SAFE_NAME.match(job_id):
        return None
    try:
        return read_job(JOBS / f'{job_id}.json')
    except (OSError, json.JSONDecodeError):
        return None


def log_tail(job_id: str) -> str:
    try:
        with open(LOGS / f'{job_id}.log', 'rb') as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - LOG_TAIL_CHARACTERS))
            return log.read().decode(errors='replace')
    except OSError:
        return ''


JOB_ACTIONS = {'hold': hold_job, 'release': release_job, 'cancel': cancel_job}


def apply_action(request: dict) -> str | None:
    """Runs a page request on the jobs it names; answers an error message, or None when it went through."""
    action, job_ids = request.get('action'), request.get('ids')
    if not isinstance(job_ids, list) or not all(isinstance(job_id, str) for job_id in job_ids):
        return 'ids must be a list of job ids'
    if action == 'priority':
        priority, order = request.get('priority'), request.get('order')
        if not isinstance(priority, int) or not 1 <= priority <= 9:
            return 'priority must be an integer from 1 to 9'
        if order is not None and not isinstance(order, (int, float)):
            return 'order must be a number'
        for job in filter(None, map(job_by_id, job_ids)):
            reprioritize_job(job, priority, order)
        return None
    if action not in JOB_ACTIONS:
        return f'unknown action: {action}'
    for job in filter(None, map(job_by_id, job_ids)):
        JOB_ACTIONS[action](job)
    if action == 'release':
        start_dispatcher()
    return None


class QueuePage(BaseHTTPRequestHandler):
    def log_message(self, *arguments) -> None:
        pass

    def answer(self, body: bytes, content_type: str, status: int = 200, cache_seconds: int = 0) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', f'max-age={cache_seconds}' if cache_seconds else 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def answer_json(self, payload, status: int = 200) -> None:
        self.answer(json.dumps(payload).encode(), 'application/json', status)

    def do_GET(self) -> None:
        parts = [unquote(part) for part in urlparse(self.path).path.split('/') if part]
        if not parts:
            self.answer(UI_PAGE.read_bytes(), 'text/html; charset=utf-8')
        elif parts == ['api', 'jobs']:
            jobs = all_jobs()
            queued = [job for job in jobs if job['state'] in ('waiting', 'held')]
            self.answer_json({'slots': slot_count(), 'paused': is_paused(), 'fair': is_fair(),
                              'missing_outputs': [job['id'] for job in jobs if lacks_outputs(job)],
                              'launch_order': [job['id'] for job in launch_order(queued, running_by_session(jobs), is_fair())],
                              'keep': settings().get('keep', DEFAULT_KEEP), 'dispatcher': is_dispatcher_running(),
                              'jobs': [job_summary(job) for job in jobs]})
        elif len(parts) == 3 and parts[:2] == ['api', 'job'] and (job := job_by_id(parts[2])):
            self.answer_json({'prompt': job.get('prompt', ''), 'log': log_tail(job['id'])})
        elif len(parts) == 3 and parts[0] == 'images' and all(SAFE_NAME.match(part) for part in parts[1:]) \
                and (image := CODEX_IMAGES / parts[1] / parts[2]).is_file():
            self.answer(image.read_bytes(), mimetypes.guess_type(image.name)[0] or 'application/octet-stream', cache_seconds=86400)
        else:
            self.answer_json({'error': 'not found'}, 404)

    def do_POST(self) -> None:
        # A JSON content type cannot be sent cross-origin without a preflight, which this server never grants.
        if self.headers.get('Content-Type') != 'application/json':
            return self.answer_json({'error': 'expected application/json'}, 415)
        try:
            request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        except ValueError:
            return self.answer_json({'error': 'invalid JSON'}, 400)
        if not isinstance(request, dict):
            return self.answer_json({'error': 'expected an object'}, 400)
        path = urlparse(self.path).path
        if path == '/api/action':
            error = apply_action(request)
        elif path == '/api/slots':
            count = request.get('count')
            error = None if isinstance(count, int) and 1 <= count <= 32 else 'count must be an integer from 1 to 32'
            if error is None:
                save_setting('slots', count)
        elif path == '/api/fair':
            is_taking_turns = request.get('fair')
            error = None if isinstance(is_taking_turns, bool) else 'fair must be true or false'
            if error is None:
                save_setting('fair', is_taking_turns)
        elif path == '/api/stop-all':
            stop_all_running()
            error = None
        elif path == '/api/retry-missing':
            retry_missing_outputs()
            error = None
        elif path == '/api/interleave':
            interleave_queue()
            error = None
        elif path == '/api/pause':
            is_pausing = request.get('paused')
            error = None if isinstance(is_pausing, bool) else 'paused must be true or false'
            if error is None:
                set_paused(is_pausing)
        else:
            return self.answer_json({'error': 'not found'}, 404)
        self.answer_json({'error': error} if error else {'ok': True}, 400 if error else 200)


def serve_page(arguments) -> None:
    server = ThreadingHTTPServer(('127.0.0.1', arguments.port), QueuePage)
    print(f'codex-queue: http://127.0.0.1:{arguments.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    adding = commands.add_parser('add')
    adding.add_argument('--priority', type=int, default=5, choices=range(1, 10))
    adding.add_argument('--label', required=True)
    adding.add_argument('--workdir', help=f'codex exec -C (default: {WORK}/<id>, kept by the queue)')
    adding.add_argument('--prompt-file', required=True)
    adding.add_argument('--add-dir', action='append', default=[])
    adding.add_argument('--skip-if-exists', nargs='*', default=[], help='output files: the job is skipped once all exist')
    adding.set_defaults(handler=add, codex_options=[])
    waiting = commands.add_parser('wait')
    waiting.add_argument('id')
    waiting.set_defaults(handler=wait)
    adding.add_argument('--session')
    adding.add_argument('--caller', help='who queued it (default: $CODEX_QUEUE_CALLER, then $CLAUDE_CODE_SESSION_ID)')
    adding.add_argument('--origin', default='.', help='the project or worktree directory the job comes from (default: here)')
    listing = commands.add_parser('list')
    listing.add_argument('--session')
    listing.add_argument('--all', action='store_true', help='also every finished job still kept')
    listing.add_argument('--since', type=duration_argument, help='also the jobs finished within this duration (30m, 2h, 1d)')
    listing.set_defaults(handler=show)

    def selector(name: str, handler) -> argparse.ArgumentParser:
        command = commands.add_parser(name)
        if name == 'priority':
            command.add_argument('priority', type=int, choices=range(1, 10))
        command.add_argument('ids', nargs='*')
        command.add_argument('--session')
        command.add_argument('--label')
        command.set_defaults(handler=handler)
        return command

    selector('priority', set_priority)
    selector('hold', hold)
    selector('release', release)
    selector('cancel', cancel)
    commands.add_parser('pause').set_defaults(handler=pause)
    taking_turns = commands.add_parser('fair')
    taking_turns.add_argument('state', nargs='?', choices=('on', 'off'))
    taking_turns.set_defaults(handler=set_fair)
    commands.add_parser('interleave').set_defaults(handler=interleave)
    commands.add_parser('retry-missing').set_defaults(handler=retry_missing)
    commands.add_parser('stop-all').set_defaults(handler=stop_all)
    commands.add_parser('resume').set_defaults(handler=resume)
    commands.add_parser('dispatch').set_defaults(handler=dispatch)
    sizing = commands.add_parser('slots')
    sizing.add_argument('count', type=int, nargs='?', choices=range(1, 33), metavar='1-32')
    sizing.set_defaults(handler=set_slots)
    keeping = commands.add_parser('keep')
    keeping.add_argument('duration', nargs='?', type=duration_argument)
    keeping.set_defaults(handler=set_keep)
    page = commands.add_parser('ui')
    page.add_argument('--port', type=int, default=DEFAULT_PORT)
    page.set_defaults(handler=serve_page)
    # Everything after `--` goes to `codex exec` as it is (-s workspace-write, -o <file>, -m <model>...).
    codex_options: list = []
    if '--' in sys.argv[1:]:
        cut = sys.argv.index('--')
        own, codex_options = sys.argv[1:cut], sys.argv[cut + 1:]
    else:
        own = sys.argv[1:]
    arguments = parser.parse_args(own)
    if arguments.command == 'add':
        arguments.codex_options = codex_options
    arguments.handler(arguments)


if __name__ == '__main__':
    main()
