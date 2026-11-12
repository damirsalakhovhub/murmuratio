"""One display-only naming rule for every provider and launch source."""
import re

LIMIT = 120
TASK_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+")
TASK_START = re.compile(r'^(?:fix|add|remove|update|build|implement|repair|restore|preserve|keep|'
                        r'clean(?:up)?|audit|review|test|refactor|improve|support|show|hide|'
                        r'create|check|verify|migrate|investigate|debug|reduce|prevent|rename|'
                        r'replace|port|sync|handle|allow|reject)\b', re.I)
MARKER = re.compile(r'<[A-Za-z][\w.:-]*(?:\s[^<>]*)?/>|</?[A-Za-z][\w.:-]*>')
SERVICE = re.compile(r'^(?:[\w-]+|time box|soft budget|allowed paths|out of scope|pre-mortem|'
                     r'owner model|reasoning effort|task id|soft time budget)\s*:', re.I)
BOILERPLATE = re.compile(
    r'^(?:at the limit\b|worktree[ :~\/]|you are a worker\b|follow the repository\b|'
    r'before committing\b|commit and push\b|reply in English\b|'
    r'never change product\b|(?:hello|hi|hey)(?:[!,.\s]|$))', re.I)
MACHINE = re.compile(r'<(environment_context|permissions instructions|collaboration_mode|'
                     r'user_instructions|system|system-reminder|task-notification)\b[^>]*>.*?</\1>', re.S)


def _lines(text):
    if not isinstance(text, str):
        return []
    # Wrapper preambles end at Task:, or at the next top-level task heading.
    text = MACHINE.sub('', text)
    task = re.search(r'^Task:\s*$', text, re.M)
    if task:
        text = text[task.end():]
    card = re.search(r'^# [^\n]+ card \((?:project rules|open-source local board for Claude Code \+ Codex agents; project created \d{4}-\d{2}-\d{2})\)$', text, re.M)
    if card:
        end = re.search(r'^# (?!AGENTS\.md\b).+', text[card.end():], re.M)
        text = text[card.end() + end.start():] if end else ''
    text = re.sub(r'^# AGENTS\.md instructions\b.*?(?:</INSTRUCTIONS>|</user_instructions>)', '', text, flags=re.S)
    lines, fenced = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = MARKER.sub('', line).strip()
        if line:
            lines.append(line)
    return lines


def _meaningful(line, explicit=False):
    line = re.sub(r'^#{1,6}\s+', '', line).strip().strip('#').strip()
    if not line or BOILERPLATE.match(line) or line in ('---', '***'):
        return ''
    if not explicit and line.lower() in ('task', 'goal', 'instructions', 'context', 'why', 'do', 'checks', 'report', 'scope'):
        return ''
    if re.match(r'^(?:claude|codex)\s*:\s*(?:task|goal|kind|mode|model|effort)\s*:', line, re.I):
        return ''
    if SERVICE.match(line):
        # An explicit human title may contain a colon. Known launch metadata never is a title.
        if explicit and not re.match(r'^(?:kind|mode|pre-mortem|budget|repo|branch|worktree|task|process|direction|goal|'
                                     r'model|effort|time box|soft budget|allowed paths|out of scope|owner model|'
                                     r'reasoning effort|task id|soft time budget)\s*:', line, re.I):
            return line
        if re.match(r'^(?:task|goal):', line, re.I):
            rest = line.split(':', 1)[1].strip()
            if len(rest.split()) >= 2 or TASK_ID.fullmatch(rest):
                return rest
        return ''
    return line


def display_name(*, titles=(), text='', project='', limit=LIMIT):
    """Return (name, truthful source), preferring recorded titles over brief text."""
    chosen, source = '', None
    fallback = None
    priority = {'desktop': 0, 'registry': 1, 'wrapper': 2, 'description': 3, 'first_user': 4}
    for origin, title in sorted(titles, key=lambda item: priority.get(item[0], 5)):
        for line in _lines(title):
            chosen = _meaningful(line, explicit=True)
            if chosen and TASK_ID.fullmatch(chosen):
                fallback = fallback or (chosen, origin)
                chosen = ''
            if chosen:
                source = origin
                break
        if chosen:
            break
    if not chosen:
        lines = _lines(text)
        headings = [line for line in lines if re.match(r'^#{1,6}\s+', line)]
        for line in headings + lines:
            chosen = _meaningful(line)
            if chosen and TASK_ID.fullmatch(chosen):
                fallback = fallback or (chosen, 'first_user')
                chosen = ''
            if chosen:
                if not headings:
                    sentence = re.split(r'(?<=[.!?])\s+(?=[A-Z])', chosen, maxsplit=1)[0]
                    if len(sentence.split()) >= 3:
                        chosen = sentence
                source = 'first_user'
                break
    if not chosen and fallback:
        chosen, source = fallback
    chosen = ' '.join(chosen.split()) or 'Untitled'
    if project:
        rest = re.sub(r'^' + re.escape(project) + r'(?:\s*[:—–-]\s*|\s+)', '', chosen, count=1, flags=re.I)
        # Keep role/session names and short fragments; remove only a task-like remainder.
        if rest != chosen and len(rest.split()) >= 2 and TASK_START.match(rest):
            chosen = rest[0].upper() + rest[1:]
    if len(chosen) > limit:
        prefix = chosen[:limit - 1]
        chosen = (prefix.rsplit(' ', 1)[0] if ' ' in prefix else prefix).rstrip(' ,;:') + '…'
    return chosen, source


def name_fields(**kwargs):
    task, source = display_name(**kwargs)
    return {'task': task, 'title_source': source}


def orchestrator_name(name):
    """Recorded session titles identify coordinators consistently across providers."""
    return isinstance(name, str) and 'orchestrator' in name.lower()
