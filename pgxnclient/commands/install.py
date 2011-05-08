"""
pgxnclient -- installation/loading commands implementation
"""

# Copyright (C) 2011 Daniele Varrazzo

# This file is part of the PGXN client

import os
import shutil
import logging
from subprocess import PIPE

from pgxnclient import Label, SemVer
from pgxnclient.i18n import _, N_
from pgxnclient.utils import sha1
from pgxnclient.errors import BadChecksum, PgxnClientException
from pgxnclient.network import download
from pgxnclient.commands import Command, WithDatabase, WithMake, WithPgConfig
from pgxnclient.commands import WithSpec, WithSpecLocal, WithSudo

logger = logging.getLogger('pgxnclient.commands')


class Download(WithSpec, Command):
    name = 'download'
    description = N_("download a distribution from the network")

    @classmethod
    def customize_parser(self, parser, subparsers, **kwargs):
        subp = super(Download, self).customize_parser(
            parser, subparsers, **kwargs)
        subp.add_argument('--target', metavar='PATH', default='.',
            help = _('Target directory and/or filename to save'))

        return subp

    def run(self):
        spec = self.get_spec()
        data = self.get_meta(spec)

        try:
            chk = data['sha1']
        except KeyError:
            raise PgxnClientException(
                "sha1 missing from the distribution meta")

        fin = self.api.download(spec.name, SemVer(data['version']))
        fn = self._get_local_file_name(fin.url)
        fn = download(fin, fn, rename=True)
        self.verify_checksum(fn, chk)
        return fn

    def verify_checksum(self, fn, chk):
        """Verify that a downloaded file has the expected sha1."""
        sha = sha1()
        logger.debug(_("checking sha1 of '%s'"), fn)
        f = open(fn, "rb")
        try:
            while 1:
                data = f.read(8192)
                if not data: break
                sha.update(data)
        finally:
            f.close()

        sha = sha.hexdigest()
        if sha != chk:
            os.unlink(fn)
            logger.error(_("file %s has sha1 %s instead of %s"),
                fn, sha, chk)
            raise BadChecksum(_("bad sha1 in downloaded file"))

    def _get_local_file_name(self, url):
        from urlparse import urlsplit
        if os.path.isdir(self.opts.target):
            basename = urlsplit(url)[2].rsplit('/', 1)[-1]
            fn = os.path.join(self.opts.target, basename)
        else:
            fn = self.opts.target

        return os.path.abspath(fn)


class InstallUninstall(WithMake, WithSpecLocal, Command):
    """
    Base class to implement the ``install`` and ``uninstall`` commands.
    """
    def run(self):
        return self.call_with_temp_dir(self._run)

    def _run(self, dir):
        spec = self.get_spec()
        if spec.is_dir():
            pdir = spec.dirname
        elif spec.is_file():
            pdir = self.unpack(spec.filename, dir)
        else:   # download
            self.opts.target = dir
            fn = Download(self.opts).run()
            pdir = self.unpack(fn, dir)

        self.maybe_run_configure(pdir)

        self._inun(pdir)

    def _inun(self, pdir):
        """Run the specific command, implemented in the subclass."""
        raise NotImplementedError

    def maybe_run_configure(self, dir):
        fn = os.path.join(dir, 'configure')
        logger.debug("checking '%s'", fn)
        # TODO: probably not portable
        if not os.path.exists(fn):
            return

        logger.info(_("running configure"))
        p = self.popen(fn, cwd=dir)
        p.communicate()
        if p.returncode:
            raise PgxnClientException(
                _("configure failed with return code %s") % p.returncode)


class Install(WithSudo, InstallUninstall):
    name = 'install'
    description = N_("download, build and install a distribution")

    def _inun(self, pdir):
        logger.info(_("building extension"))
        self.run_make('all', dir=pdir)

        logger.info(_("installing extension"))
        self.run_make('install', dir=pdir, sudo=self.opts.sudo)


class Uninstall(WithSudo, InstallUninstall):
    name = 'uninstall'
    description = N_("remove a distribution from the system")

    def _inun(self, pdir):
        logger.info(_("removing extension"))
        self.run_make('uninstall', dir=pdir, sudo=self.opts.sudo)


class Check(WithDatabase, InstallUninstall):
    name = 'check'
    description = N_("run a distribution's test")

    def _inun(self, pdir):
        logger.info(_("checking extension"))
        upenv = self.get_psql_env()
        logger.debug("additional env: %s", upenv)
        env = os.environ.copy()
        env.update(upenv)

        cmd = ['installcheck']
        if 'PGDATABASE' in upenv:
            cmd.append("CONTRIB_TESTDB=" +  env['PGDATABASE'])

        try:
            self.run_make(cmd, dir=pdir, env=env)
        except PgxnClientException:
            # if the test failed, copy locally the regression result
            for ext in ('out', 'diffs'):
                fn = os.path.join(pdir, 'regression.' + ext)
                if os.path.exists(fn):
                    logger.info(_('copying regression.%s'), ext)
                    shutil.copy(fn, './regression.' + ext)
            raise


class LoadUnload(WithPgConfig, WithDatabase, WithSpecLocal, Command):
    """
    Base class to implement the ``load`` and ``unload`` commands.
    """
    def get_pg_version(self):
        """Return the version of the selected database."""
        data = self.call_psql('SELECT version();')
        pgver = self.parse_pg_version(data)
        logger.debug("PostgreSQL version: %d.%d.%d", *pgver)
        return pgver

    def parse_pg_version(self, data):
        import re
        m = re.match(r'\S+\s+(\d+)\.(\d+)(?:\.(\d+))?', data)
        if m is None:
            raise PgxnClientException(
                "cannot parse version number from '%s'" % data)

        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))

    def is_extension(self, name):
        fn = os.path.join(self.call_pg_config('sharedir'),
            "extension", name + ".control")
        logger.debug("checking if exists %s", fn)
        return os.path.exists(fn)

    def call_psql(self, command):
        cmdline = [self.find_psql()]
        cmdline.extend(self.get_psql_options())
        if command is not None:
            cmdline.append('-tA')   # tuple only, unaligned
            cmdline.extend(['-c', command])

        logger.debug("calling %s", cmdline)
        p = self.popen(cmdline, stdout=PIPE)
        out, err = p.communicate()
        if p.returncode:
            raise PgxnClientException(
                "psql returned %s running command" % (p.returncode))

        return out

    def load_sql(self, filename=None, data=None):
        cmdline = [self.find_psql()]
        cmdline.extend(self.get_psql_options())
        # load via pipe to enable psql commands in the file
        if not data:
            fin = open(filename, 'r')
            p = self.popen(cmdline, stdin=fin)
            p.communicate()
        else:
            p = self.popen(cmdline, stdin=PIPE)
            p.communicate(data)

        if p.returncode:
            raise PgxnClientException(
                "psql returned %s loading extension" % (p.returncode))

    def find_psql(self):
        return self.call_pg_config('bindir') + '/psql'

    def find_sql_file(self, name, sqlfile):
        # In the extension the sql can be specified with a directory,
        # butit gets flattened into the target dir by the Makefile
        sqlfile = os.path.basename(sqlfile)

        sharedir = self.call_pg_config('sharedir')
        # TODO: we only check in contrib and in <name>: actually it may be
        # somewhere else - only the makefile knows!
        tries = [
            name + '/' + sqlfile,
            sqlfile.rsplit('.', 1)[0] + '/' + sqlfile,
            'contrib/' + sqlfile,
        ]
        tried = set()
        for fn in tries:
            if fn in tried:
                continue
            tried.add(fn)
            fn = sharedir + '/' + fn
            logger.debug("checking sql file in %s" % fn)
            if os.path.exists(fn):
                return fn
        else:
            raise PgxnClientException(
                "cannot find sql file for extension '%s': '%s'"
                % (name, sqlfile))

    def _register_loaded(self, fn):
        if not hasattr(self, '_loaded'):
            self._loaded = []

        self._loaded.append(fn)

    def _is_loaded(self, fn):
        return hasattr(self, '_loaded') and fn in self._loaded


class Load(LoadUnload):
    name = 'load'
    description = N_("load a distribution's extensions into a database")

    def run(self):
        spec = self.get_spec()
        dist = self.get_meta(spec)

        # TODO: probably unordered before Python 2.7 or something
        for name, data in dist['provides'].items():
            sql = data.get('file')
            self.load_ext(name, sql)

    def load_ext(self, name, sqlfile):
        logger.debug(_("loading extension '%s' with file: %s"),
            name, sqlfile)

        if sqlfile and not sqlfile.endswith('.sql'):
            logger.info(
                _("the specified file '%s' doesn't seem SQL:"
                    " assuming '%s' is not a PostgreSQL extension"),
                    sqlfile, name)
            return

        pgver = self.get_pg_version()

        if pgver >= (9,1,0):
            if self.is_extension(name):
                self.create_extension(name)
                return
            else:
                self.confirm(_("""\
The extension '%s' doesn't contain a control file:
it will be installed as a loose set of objects.
Do you want to continue?""")
                    % name)

        confirm = False
        if not sqlfile:
            sqlfile = name + '.sql'
            confirm = True

        fn = self.find_sql_file(name, sqlfile)
        if confirm:
            self.confirm(_("""\
The extension '%s' doesn't specify a SQL file.
'%s' is probably the right one.
Do you want to load it?""")
                % (name, fn))

        if self._is_loaded(fn):
            logger.info(_("file %s already loaded"), fn)
        else:
            self.load_sql(fn)
            self._register_loaded(fn)

    def create_extension(self, name):
        # TODO: namespace etc.
        cmd = "CREATE EXTENSION %s;" % Label(name)
        self.load_sql(data=cmd)


class Unload(LoadUnload):
    name = 'unload'
    description = N_("unload a distribution's extensions from a database")

    def run(self):
        spec = self.get_spec()
        dist = self.get_meta(spec)

        # TODO: ensure ordering
        provs = dist['provides'].items()
        provs.reverse()
        for name, data in provs:
            sql = data.get('file')
            self.load_ext(name, sql)

    def load_ext(self, name, sqlfile):
        logger.debug(_("unloading extension '%s' with file: %s"),
            name, sqlfile)

        if sqlfile and not sqlfile.endswith('.sql'):
            logger.info(
                _("the specified file '%s' doesn't seem SQL:"
                    " assuming '%s' is not a PostgreSQL extension"),
                    sqlfile, name)
            return

        pgver = self.get_pg_version()

        if pgver >= (9,1,0):
            if self.is_extension(name):
                self.drop_extension(name)
                return
            else:
                self.confirm(_("""\
The extension '%s' doesn't contain a control file:
will look for an SQL script to unload the objects.
Do you want to continue?""")
                    % name)

        if not sqlfile:
            sqlfile = name + '.sql'

        sqlfile = 'uninstall_' + sqlfile

        fn = self.find_sql_file(name, sqlfile)
        self.confirm(_("""\
In order to unload the extension '%s' looks like you will have
to load the file '%s'.
Do you want to execute it?""")
                % (name, fn))

        self.load_sql(fn)

    def drop_extension(self, name):
        # TODO: namespace etc.
        # TODO: cascade
        cmd = "DROP EXTENSION %s;" % Label(name)
        self.load_sql(data=cmd)
