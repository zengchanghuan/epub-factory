#!/usr/bin/env python3
"""Render reviewed systemd migration drafts; never installs, reads .env, or runs services."""
import argparse
import re
from pathlib import Path


def safe_path(value):
    # Deliberately avoid systemd specifiers, shell expansion and quoting ambiguity.
    if value == '/' or not value.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        raise ValueError('Use an absolute path without whitespace, specifiers or shell metacharacters')
    if '..' in Path(value).parts:
        raise ValueError('Parent traversal is not allowed')
    return value.rstrip('/')


def render_units(*, project_dir, user, group='', environment_files, working_directory=None):
    project = safe_path(project_dir)
    for account in (user, group):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*\$?|[0-9]+', account) and account:
            raise ValueError('Invalid systemd user/group')
    if not user or not environment_files:
        raise ValueError('Existing worker User and EnvironmentFile paths are required')
    env_lines = '\n'.join('EnvironmentFile=' + ('-' if path.startswith('-') else '')
                          + safe_path(path[1:] if path.startswith('-') else path)
                          for path in environment_files)
    working_directory = safe_path(working_directory or project + '/backend')
    python = project + '/backend/.venv/bin/python'
    group_line = f'Group={group}\n' if group else ''
    return {
        'epub-factory-worker.service.d/90-explicit-role.conf': (
            '# Review with the existing unit and all drop-ins; do not replace its custom settings.\n'
            '[Service]\nEnvironment=NOSETPS=1\nExecStart=\n'
            f'ExecStart={python} -m app.infra.worker book\n'
        ),
        'epub-factory-housekeeping.service': (
            '# Draft: review against the original worker for custom limits, environment and hardening.\n'
            '[Unit]\nDescription=FixEpub housekeeping worker\n'
            'After=network-online.target\nWants=network-online.target\n\n'
            '[Service]\nType=simple\n'
            f'User={user}\n{group_line}WorkingDirectory={working_directory}\n'
            f'{env_lines}\nEnvironment=NOSETPS=1\n'
            f'ExecStart={python} -m app.infra.worker housekeeping\n'
            'Restart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=1900\n\n'
            '[Install]\nWantedBy=multi-user.target\n'
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', required=True)
    parser.add_argument('--user', required=True, help='Copy the effective User from the existing worker')
    parser.add_argument('--group', default='', help='Copy Group if explicitly configured')
    parser.add_argument('--working-directory', required=True, help='Copy the existing worker WorkingDirectory')
    parser.add_argument('--environment-file', action='append', required=True,
                        help='Existing EnvironmentFile path (repeat in the same order; contents never read)')
    parser.add_argument('--output-dir', type=Path, help='Write drafts here; default is preview only')
    args = parser.parse_args(argv)
    try:
        units = render_units(project_dir=args.project_dir, user=args.user, group=args.group,
                             environment_files=args.environment_file, working_directory=args.working_directory)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output_dir is None:
        for name, content in units.items():
            print(f'# --- {name} ---\n{content}')
        return 0
    # Refuse all overwrites before writing any draft; never targets /etc implicitly.
    targets = [(args.output_dir / name, content) for name, content in units.items()]
    if any(path.exists() or path.is_symlink() for path, _ in targets):
        parser.error('Output already exists; choose a fresh review directory')
    for path, content in targets:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(content)
    print(f'Wrote {len(targets)} review drafts to {args.output_dir}; nothing was installed or started.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
