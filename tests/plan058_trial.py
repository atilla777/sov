"""Disposable installation/update trial; does not touch installed projects."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
OLD = 'a849ddd10e5576e8013137215e1ea0e5454297d5'
NEW = 'b6a6e8b32f04df3f3e9527bed8ae7db0dc402279'
COMMANDS = ('sov', 'sov-feature', 'sov-bug', 'sov-fast', 'sov-decompose', 'sov-retro')


def run(*args, cwd=None, code=0):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout.strip()


def preflight(source, installed_command, old_command):
    if (source / 'ROADMAP.md').exists() and not (source / 'tasks').exists():
        raise RuntimeError('legacy task source requires an explicit decision')
    if installed_command.read_bytes() != old_command.read_bytes():
        raise RuntimeError('user-edited command requires an explicit decision')


def config(project, release, binary):
    (project / 'opencode.json').write_text(json.dumps({
        '$schema': 'https://opencode.ai/config.json',
        'skills': {'paths': [str(release / 'skills')]},
    }), encoding='utf-8')
    agents = project / '.opencode' / 'agents'
    commands = project / '.opencode' / 'commands'
    agents.mkdir(parents=True, exist_ok=True)
    commands.mkdir(parents=True, exist_ok=True)
    for name in ('sov-standard', 'sov-advanced'):
        shutil.copy2(release / 'templates' / 'opencode' / 'agents' / (name + '.md'), agents / (name + '.md'))
    for name in COMMANDS:
        shutil.copy2(release / '.opencode' / 'commands' / (name + '.md'), commands / (name + '.md'))
    existing = (project / 'AGENTS.md').read_text(encoding='utf-8')
    (project / 'AGENTS.md').write_text(existing + '\n## Подключение SOV\n'
        f'- Выпуск: `{run("git", "rev-parse", "HEAD", cwd=release)}`; checkout `{release}`.\n'
        f'- Основная копия: `{project}`; общее состояние `{project / ".sov"}`. '
        'Во всех worktree используй одну `.sov/` основной копии.\n'
        f'- Задачи: файловый `sov-tasks`, бинарник `{binary}`; '
        f'`"{binary}" --state-dir "{project / ".sov"}" validate`.\n'
        '- `/sov` запускает `sov-orchestrator`; прочие `/sov*` используют тот же маршрут.\n'
        f'- `skills.paths`: `{release / "skills"}`; команды: `{commands}`; '
        f'субагенты: `{agents}`; модели: Luna (standard), Sol (advanced).\n'
        '- Правила: [rules/index.md](rules/index.md); требования: [docs/index.md](docs/index.md).\n'
        '- Проверки: `git diff --check`, `sov-task validate`; remote `origin`, ветка `main`; '
        'публикация прямым push после проверок, сверка remote SHA.\n', encoding='utf-8')


def check(project, worktree, release, binary):
    state = project / '.sov'
    for location in (project, worktree):
        settings = json.loads(run('opencode', 'debug', 'config', cwd=location))
        assert str(release / 'skills') in settings['skills']['paths']
        assert all(name in settings['command'] for name in COMMANDS)
        for name in ('sov-standard', 'sov-advanced'):
            agent = json.loads(run('opencode', 'debug', 'agent', name, cwd=location))
            assert agent['model']['modelID'] in ('gpt-6-luna', 'gpt-6-sol'), agent
        cli = lambda *args: json.loads(run(str(binary), '--state-dir', str(state), *args, cwd=location))
        assert cli('validate')['valid'] is True
        assert len(cli('list')) == 1
        assert cli('show', '0001')['id'] == '0001'
        assert cli('list-completed') == []
        assert (release / 'templates' / 'task.md').is_file()
        assert (release / 'skills' / 'sov-orchestrator' / 'SKILL.md').is_file()
    assert not (worktree / '.sov').exists()


def main():
    with tempfile.TemporaryDirectory(prefix='sov-plan058-') as folder:
        root = Path(folder)
        old, new = root / 'sov-old', root / 'sov-new'
        run('git', 'clone', '--quiet', '--local', '--no-hardlinks', str(SOURCE), str(old))
        run('git', 'checkout', '--quiet', '--detach', OLD, cwd=old)
        run('git', 'clone', '--quiet', '--local', '--no-hardlinks', str(SOURCE), str(new))
        run('git', 'checkout', '--quiet', '--detach', NEW, cwd=new)
        project, worktree, remote = root / 'project', root / 'worktree', root / 'remote.git'
        run('git', 'init', '--bare', '--initial-branch=main', str(remote))
        run('git', 'init', '--initial-branch=main', str(project))
        run('git', 'remote', 'add', 'origin', str(remote), cwd=project)
        for directory in ('rules', 'docs', '.sov/tasks', '.sov/claims', '.sov/completed', '.sov/archive'):
            (project / directory).mkdir(parents=True)
        (project / 'AGENTS.md').write_text('# Пробный проект\n\nПользовательское правило: беречь заметки.\n', encoding='utf-8')
        (project / 'rules/index.md').write_text('# Правила\n\nСледуй AGENTS.md.\n', encoding='utf-8')
        (project / 'docs/index.md').write_text('# Требования\n\nПробный проект.\n', encoding='utf-8')
        (project / '.gitignore').write_text('.sov/\n', encoding='utf-8')
        binary = root / 'bin' / 'sov-task'
        binary.parent.mkdir()
        run('go', 'test', '-race', './...', cwd=old / 'skills/sov-tasks')
        run('go', 'build', '-o', str(binary.parent / '.sov-task-next'), '.', cwd=old / 'skills/sov-tasks')
        os.replace(binary.parent / '.sov-task-next', binary)
        config(project, old, binary)
        assert 'Пользовательское правило: беречь заметки.' in (project / 'AGENTS.md').read_text()
        assert run('git', 'check-ignore', '.sov/tasks/0001-probe/task.yaml', cwd=project)
        card = root / 'card.yaml'
        card.write_text('title: Trial task\ntype: non_development\nexecutor: agent\n'
                        'description: Confirm installation\ndepends_on: []\n'
                        'acceptance_criteria: [Visible from both copies]\n', encoding='utf-8')
        cli = lambda *args: json.loads(run(str(binary), '--state-dir', str(project / '.sov'), *args, cwd=project))
        assert cli('create', '--file', str(card), '--slug', 'probe')['id'] == '0001'
        run('git', 'add', '.', cwd=project)
        commit = ('git', '-c', 'user.name=Trial', '-c', 'user.email=trial@example.invalid', 'commit')
        run(*commit, '-m', 'Install pinned SOV', cwd=project)
        run('git', 'push', 'origin', 'main', cwd=project)
        run('git', 'worktree', 'add', '--quiet', '-b', 'trial', str(worktree), cwd=project)
        check(project, worktree, old, binary)
        print('FIRST INSTALL: commands, models, task, paths, shared state: OK')

        # Simulated stopped writers: no OpenCode process is kept alive by this test.
        backup = root / 'state-backup'
        shutil.copytree(project / '.sov', backup)
        run('go', 'test', '-race', './...', cwd=new / 'skills/sov-tasks')
        run('go', 'build', '-o', str(binary.parent / '.sov-task-next'), '.', cwd=new / 'skills/sov-tasks')
        assert json.loads(run(str(binary.parent / '.sov-task-next'), '--state-dir', str(project / '.sov'), 'validate'))['valid']
        assert (backup / 'tasks').is_dir()
        # Inject a user edit in a same-named command: do not overwrite on conflict.
        command = project / '.opencode/commands/sov.md'
        original = command.read_text(encoding='utf-8')
        command.write_text(original + '\nПользовательская правка\n', encoding='utf-8')
        assert command.read_text() != (old / '.opencode/commands/sov.md').read_text()
        assert command.read_text().endswith('Пользовательская правка\n')
        try:
            preflight(project / '.sov', command, old / '.opencode/commands/sov.md')
        except RuntimeError as error:
            assert 'user-edited command' in str(error)
        else:
            raise AssertionError('user edit was not blocked')
        assert cli('show', '0001')['id'] == '0001'  # old state stays visible
        command.write_text(original, encoding='utf-8')  # fixture resolves the conflict
        preflight(project / '.sov', command, old / '.opencode/commands/sov.md')
        os.replace(binary.parent / '.sov-task-next', binary)
        config(project, new, binary)
        run('git', 'add', '.', cwd=project)
        run(*commit, '-m', 'Update pinned SOV', cwd=project)
        run('git', 'push', 'origin', 'main', cwd=project)
        run('git', 'merge', '--ff-only', 'main', cwd=worktree)
        check(project, worktree, new, binary)
        for name, token in (('sov-standard', 'STANDARD-OK'), ('sov-advanced', 'ADVANCED-OK')):
            reply = run('opencode', 'run', '--dir', str(worktree), '--agent', name,
                        '--model', 'openai/gpt-6-luna' if name == 'sov-standard' else 'openai/gpt-6-sol',
                        'Diagnostic only; do not edit files or claim tasks. Reply exactly ' + token + '.', cwd=worktree)
            assert token in reply, (name, reply)
            print('LIVE MODEL:', name, token)
        assert 'Пользовательское правило: беречь заметки.' in (worktree / 'AGENTS.md').read_text()
        assert (backup / 'tasks' / '0001-probe' / 'task.yaml').read_bytes() == (
            project / '.sov/tasks/0001-probe/task.yaml').read_bytes()
        assert run('git', '--git-dir', str(remote), 'rev-parse', 'main') == run('git', 'rev-parse', 'HEAD', cwd=project)
        print('UPDATE: candidate, backup, same task in both copies, remote, user text: OK')

        # Incompatible historical store: stop before changing resources or data.
        legacy = root / 'legacy-state'
        legacy.mkdir()
        (legacy / 'ROADMAP.md').write_text('| ID | Status |\n| --- | --- |\n| OLD-001 | active |\n')
        try:
            preflight(legacy, command, new / '.opencode/commands/sov.md')
        except RuntimeError as error:
            assert 'legacy task source' in str(error)
        else:
            raise AssertionError('legacy store was not blocked')
        assert cli('list')[0]['id'] == '0001'
        print('STOP: user-edited command and legacy ROADMAP store detected before switching: OK')


if __name__ == '__main__':
    main()
