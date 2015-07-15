"""Shim to install packages using flit metadata on Python 2
"""

import argparse
import configparser
import os
import shutil
import site
from subprocess import check_call
import sys
import tempfile

pjoin = os.path.join

__version__ = '0.1'

class Module(object):
    """This represents the module/package that we are going to distribute
    """
    def __init__(self, name, directory='.'):
        self.name = name

        # It must exist either as a .py file or a directory, but not both
        pkg_dir = pjoin(directory, name)
        py_file = pjoin(directory, name+'.py')
        if os.path.isdir(pkg_dir) and os.path.isfile(py_file):
            raise ValueError("Both {} and {} exist".format(pkg_dir, py_file))
        elif os.path.isdir(pkg_dir):
            self.path = pkg_dir
            self.is_package = True
        elif os.path.isfile(py_file):
            self.path = py_file
            self.is_package = False
        else:
            raise ValueError("No file/folder found for module {}".format(name))

    @property
    def file(self):
        if self.is_package:
            return pjoin(self.path, '__init__.py')
        else:
            return self.path

# For the directories where we'll install stuff
_interpolation_vars = {
    'userbase': site.USER_BASE,
    'usersite': site.USER_SITE,
    'py_major': sys.version_info[0],
    'py_minor': sys.version_info[1],
    'prefix'  : sys.prefix,
}

def _requires_dist_to_pip_requirement(requires_dist):
    """Parse "Foo (v); python_version == '2.x'" from Requires-Dist

    Returns pip-style appropriate for requirements.txt.
    """
    env_mark = ''
    if ';' in requires_dist:
        name_version, env_mark = requires_dist.split(';', 1)
    else:
        name_version = requires_dist
    if '(' in name_version:
        # turn 'name (X)' and 'name (<X.Y)'
        # into 'name == X' and 'name < X.Y'
        name, version = name_version.split('(', 1)
        name = name.strip()
        version = version.replace(')', '').strip()
        if not any(c in version for c in '=<>'):
            version = '==' + version
        name_version = name + version
    # re-add environment marker
    return ';'.join([name_version, env_mark])

def get_dirs(user=True):
    """Get the 'scripts' and 'purelib' directories we'll install into.

    This is an abbreviated version of distutils.command.install.INSTALL_SCHEMES
    """
    if user:
        purelib = site.USER_SITE
        if sys.platform == 'win32':
            scripts = "{userbase}/Python{py_major}{py_minor}/Scripts"
        else:
            scripts = "{userbase}/bin"
    elif sys.platform == 'win32':
        scripts = "{prefix}/Scripts",
        purelib = "{prefix}/Lib/site-packages"
    else:
        scripts = "{prefix}/bin"
        purelib = "{prefix}/lib/python{py_major}.{py_minor}/site-packages"

    return {
        'scripts': scripts.format(**_interpolation_vars),
        'purelib': purelib.format(**_interpolation_vars),
    }

script_template = """\
#!{interpreter}
from {module} import {func}
if __name__ == '__main__':
    {func}()
"""

class RootInstallError(Exception):
    def __str__(self):
        return ("Installing packages as root is not recommended. "
            "To allow this, set FLIT_ROOT_INSTALL=1 and try again.")


class Installer(object):
    def __init__(self, ini_path, user=None, symlink=False):
        self.cfg = configparser.ConfigParser()
        self.cfg.read([ini_path])
        self.module = Module(self.cfg.get('metadata', 'module'))

        if user is None:
            self.user = site.ENABLE_USER_SITE
        else:
            self.user = user

        if (os.getuid() == 0) and (not os.environ.get('FLIT_ROOT_INSTALL')):
            raise RootInstallError

        self.symlink = symlink

    def install_scripts(self, script_defs, scripts_dir):
        for name, ep in script_defs.items():
            module, func = ep.split(':', 1)
            script_file = pjoin(scripts_dir, name)
            with open(script_file, 'w') as f:
                f.write(script_template.format(
                    interpreter=sys.executable,
                    module=module,
                    func=func
                ))
            os.chmod(script_file, 0o755)

            if sys.platform == 'win32':
                cmd_file = script_file.with_suffix('.cmd')
                cmd = '"{python}" "%~dp0\{script}" %*\r\n'.format(
                            python=sys.executable, script=name)

                with open(cmd_file, 'w') as f:
                    f.write(cmd)

    def install_requirements(self):
        """Install requirements of a package with pip.

        Creates a temporary requirements.txt from requires_dist metadata.
        """
         # construct the full list of requirements, including dev requirements
        requires_dist = self.cfg.get('metadata', 'requires', fallback='').splitlines()
        dev_requires = self.cfg.get('metadata', 'dev-requires', fallback='').splitlines()
        requirements = requires_dist + dev_requires

        if not requirements:
            return

        requirements = [
            _requires_dist_to_pip_requirement(req_d)
            for req_d  in requirements
        ]
        cmd = [sys.executable, '-m', 'pip', 'install']
        if self.user:
            cmd.append('--user')
        with tempfile.NamedTemporaryFile(mode='w',
                                         suffix='requirements.txt',
                                         delete=False) as tf:
            tf.file.write('\n'.join(requirements))
        cmd.extend(['-r', tf.name])
        try:
            check_call(cmd)
        finally:
            os.remove(tf.name)

    def install(self):
        """Install a module/package into site-packages, and create its scripts.
        """
        dirs = get_dirs(user=self.user)
        try:
            os.makedirs(dirs['purelib'])
        except OSError:
            pass

        try:
            os.makedirs(dirs['scripts'])
        except OSError:
            pass

        dst = os.path.join(dirs['purelib'], os.path.basename(self.module.path))
        if os.path.lexists(dst):
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.unlink(dst)

        self.install_requirements()

        src = self.module.path
        if self.symlink:
            os.symlink(os.path.realpath(self.module.path), dst)
        elif os.path.isdir(self.module.path):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

        if 'scripts' in self.cfg:
            scripts = self.cfg['scripts']
            self.install_scripts(scripts, dirs['scripts'])

def main():
    ap = argparse.ArgumentParser('flit-install-py2', version=__version__)
    ap.add_argument('-f', '--ini-file', default='flit.ini')
    ap.add_argument('-s', '--symlink', action='store_true')
    opts = ap.parse_args()

    Installer(opts.ini_file, symlink=opts.symlink).install()

if __name__ == '__main__':
    main()
