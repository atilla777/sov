"""Check project-local command installation across the old and new layouts."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
OLD = 'b338a80b3eddd09d91b83e352301a3a83a6d6e84'
COMMANDS = ('sov', 'sov-feature', 'sov-bug', 'sov-fast', 'sov-decompose', 'sov-retro')


def run(*args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def old_command(name):
    return subprocess.run(
        ('git', 'show', f'{OLD}:.opencode/commands/{name}.md'),
        cwd=SOURCE, capture_output=True, check=True,
    ).stdout


def install(project, commands, agents):
    for name in COMMANDS:
        old = old_command(name)
        installed = commands / f'{name}.md'
        if installed.exists() and installed.read_bytes() != old:
            raise RuntimeError(f'user-edited command: {name}')
    for name in COMMANDS:
        shutil.copy2(SOURCE / 'templates/opencode/commands' / f'{name}.md', commands / f'{name}.md')
    for name in ('sov-standard', 'sov-advanced'):
        shutil.copy2(SOURCE / 'templates/opencode/agents' / f'{name}.md', agents / f'{name}.md')
    (project / 'opencode.json').write_text(json.dumps({
        '$schema': 'https://opencode.ai/config.json',
        'skills': {'paths': [str(SOURCE / 'skills')]},
    }), encoding='utf-8')
    agents_text = (project / 'AGENTS.md').read_text(encoding='utf-8')
    (project / 'AGENTS.md').write_text(
        agents_text + '\n## Подключение SOV\n'
        + f'- Команды: `{commands}`; скилы: `{SOURCE / "skills"}`.\n',
        encoding='utf-8',
    )


def main():
    assert not list((SOURCE / '.opencode/commands').glob('sov*.md'))
    documents = [SOURCE / 'README.md', SOURCE / 'docs/sov-spec.md',
                 SOURCE / 'docs/install-filesystem-tasks.md',
                 SOURCE / 'templates/README.md', SOURCE / 'templates/opencode/README.md']
    documents += [SOURCE / 'skills' / name / 'SKILL.md' for name in
                  ('sov-spec', 'sov-plan', 'sov-research', 'sov-orchestrator', 'sov-update')]
    for doc in documents:
        for link in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
            target = link.split('#', 1)[0]
            if target and not target.startswith(('https://', 'http://', '/')):
                assert (doc.parent / target).exists(), (doc, link)
    for folder, files in {
        'sov-spec': ('spec', 'adr'), 'sov-research': ('research',),
        'sov-plan': ('plan',), 'sov-orchestrator': ('task',),
    }.items():
        for name in files:
            assert (SOURCE / 'skills' / folder / 'templates' / f'{name}.md').is_file()
    assert (SOURCE / 'templates/checks.md').is_file()
    assert (SOURCE / 'templates/review.md').is_file()
    assert (SOURCE / 'templates/AGENTS.md').is_file()

    with tempfile.TemporaryDirectory(prefix='sov-plan059-') as tmp:
        project = Path(tmp) / 'project'
        project.mkdir()
        commands = project / '.opencode/commands'
        agents = project / '.opencode/agents'
        commands.mkdir(parents=True)
        agents.mkdir(parents=True)
        (project / 'AGENTS.md').write_text('# Проект\n\nПользовательское правило.\n', encoding='utf-8')
        for name in COMMANDS:
            (commands / f'{name}.md').write_bytes(old_command(name))
        edited = commands / 'sov.md'
        edited.write_bytes(edited.read_bytes() + b'\nUser change\n')
        try:
            install(project, commands, agents)
        except RuntimeError as error:
            assert 'user-edited command: sov' in str(error)
        else:
            raise AssertionError('user edit overwritten')
        assert edited.read_bytes().endswith(b'User change\n')
        edited.write_bytes(old_command('sov'))
        install(project, commands, agents)
        assert 'Пользовательское правило.' in (project / 'AGENTS.md').read_text(encoding='utf-8')
        for name in COMMANDS:
            assert (commands / f'{name}.md').read_bytes() == (
                SOURCE / 'templates/opencode/commands' / f'{name}.md').read_bytes()
        run('git', 'init', '--initial-branch=main', str(project), cwd=tmp)
        run('git', 'add', '.', cwd=project)
        run('git', '-c', 'user.name=Trial', '-c', 'user.email=trial@example.invalid',
            'commit', '-m', 'Install SOV', cwd=project)
        worktree = Path(tmp) / 'worktree'
        run('git', 'worktree', 'add', '--quiet', '-b', 'trial', str(worktree), cwd=project)
        for location in (project, worktree):
            settings = json.loads(run('opencode', 'debug', 'config', cwd=location))
            assert str(SOURCE / 'skills') in settings['skills']['paths']
            for name in COMMANDS:
                assert name in settings['command'], name
                assert 'sov-orchestrator' in settings['command'][name]['template']
            for name in ('sov-standard', 'sov-advanced'):
                assert json.loads(run('opencode', 'debug', 'agent', name, cwd=location))['mode'] == 'subagent'
        fresh = Path(tmp) / 'fresh'
        fresh.mkdir()
        fresh_commands = fresh / '.opencode/commands'
        fresh_agents = fresh / '.opencode/agents'
        fresh_commands.mkdir(parents=True)
        fresh_agents.mkdir(parents=True)
        (fresh / 'AGENTS.md').write_text('# Fresh project\n', encoding='utf-8')
        install(fresh, fresh_commands, fresh_agents)
        settings = json.loads(run('opencode', 'debug', 'config', cwd=fresh))
        assert all(name in settings['command'] for name in COMMANDS)
        print('PLAN-059: first install, old command conflict, update and worktree discovery OK')


if __name__ == '__main__':
    main()
