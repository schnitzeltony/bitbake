"""
BitBake 'Fetch' crateindex implementation

git fetcher support the SRC_URI with format of:
SRC_URI = "crateindex://some.host/somepath.git;OptionA=xxx;OptionB=xxx;..."

CrateIndex derives from Git fetcher.
It leaves downloaded index with same layout as cargo would have done by
cargo fetch:

* Git sources are fetch only: No remote / no commits / no files
* The files of the repository are copied to subfolder .cache. The subfolder
  should be set by destsuffix as:
  <cargo-home>/registry/index/<cargo-cash-hash>

Supported SRC_URI options are same as for git fetcher except that

* nocheckout
* bareclone

are ignored.

"""

# Copyright (C) 2021 Andreas Müller
#
# SPDX-License-Identifier: GPL-2.0-only
#

import collections
import errno
import os
import re
import shlex
import tempfile
import json
import bb
from   bb.fetch2 import runfetchcmd
from . import git

class CrateIndex(git.Git):
    def supports(self, ud, d):
        """
        Check to see if a given url can be fetched with git.
        """
        return ud.type in ['crateindex']

    def unpack(self, ud, destdir, d):
        """ unpack the downloaded src to destdir"""

        subdir = ud.parm.get("subpath", "")
        if subdir != "":
            def_destsuffix = "%s/" % os.path.basename(subdir.rstrip('/'))
        else:
            def_destsuffix = "git/"

        destsuffix = ud.parm.get("destsuffix", def_destsuffix)
        destdir = ud.destdir = os.path.join(destdir, destsuffix)
        destcachedir = os.path.join(destdir, '.cache')
        tmp_cachedir = tempfile.TemporaryDirectory()
        commit_id = ud.revisions[ud.names[0]]
        if os.path.exists(destdir):
            bb.utils.prunedir(destdir)

        need_lfs = self._need_lfs(ud)

        if not need_lfs:
            ud.basecmd = "GIT_LFS_SKIP_SMUDGE=1 " + ud.basecmd

        source_found = False
        source_error = []

        if not source_found:
            clonedir_is_up_to_date = not self.clonedir_need_update(ud, d)
            if clonedir_is_up_to_date:
                bb.utils.mkdirhier(destdir)
                runfetchcmd("%s init" % ud.basecmd, d, workdir=destdir)
                runfetchcmd("%s fetch %s %s" % (ud.basecmd, ud.clonedir, commit_id), d, workdir=destdir)
                # prepare cache
                runfetchcmd("%s archive %s | tar -x -C %s" % (ud.basecmd, commit_id, tmp_cachedir.name), d, workdir=destdir)
                source_found = True
            else:
                source_error.append("clone directory not available or not up to date: " + ud.clonedir)

        if not source_found:
            if ud.shallow:
                if os.path.exists(ud.fullshallow):
                    # prepare cache from shallow - will cargo accept this?
                    runfetchcmd("tar -xzf %s" % ud.fullshallow, d, workdir=tmp_cachedir.name)
                    source_found = True
                else:
                    source_error.append("shallow clone not available: " + ud.fullshallow)
            else:
                source_error.append("shallow clone not enabled")

        if not source_found:
            raise bb.fetch2.UnpackError("No up to date source found: " + "; ".join(source_error), ud.url)

        repourl = self._get_repo_url(ud)
        #runfetchcmd("%s remote set-url origin %s" % (ud.basecmd, shlex.quote(repourl)), d, workdir=destdir)

        # LFS is not to expect but just in case
        if self._contains_lfs(ud, d, destdir):
            if need_lfs and not self._find_git_lfs(d):
                raise bb.fetch2.FetchError("Repository %s has LFS content, install git-lfs on host to download (or set lfs=0 to ignore it)" % (repourl))
            elif not need_lfs:
                bb.note("Repository %s has LFS content but it is not being fetched" % (repourl))

        # Convert cache text files to the format cargo expects:
        # <0x01>
        # <ASCII representation of git hash><0x00>
        # per line
        #  <ASCII semver for fast search><0x00><JSON contents of crate index file><0x00>
        #
        # Note: cargo creates index cache for those files only necessary. We can do
        # similar
        for path_read, fdummy, files in os.walk(tmp_cachedir.name):
            path_write = path_read.replace(tmp_cachedir.name, destcachedir, 1)
            bb.utils.mkdirhier(path_write)
            for file in files:
                if file == "config.json":
                    continue
                filename_read = os.path.join(path_read, file)
                filename_write = os.path.join(path_write, file)
                with open(filename_read, 'r') as file:
                    crate_info = file.read()
                with open(filename_write, 'wb') as file:
                    file.write(b'\x01')
                    file.write(commit_id.encode('utf-8'))
                    file.write(b'\x00')
                    for line in crate_info.splitlines():
                        line = line.replace(' ', '')
                        if line != '':
                            # TODO: json is slow but detects errors (and there are some)
                            try:
                                jdict = json.loads(line)
                            except json.JSONDecodeError:
                                bb.note("Invalid json line in %s - ignore" % (filename_write))
                                continue
                            else:
                                if 'vers' in jdict:
                                    file.write(jdict['vers'].encode("utf-8"))
                                    file.write(b'\x00')
                                    file.write(line.encode('utf-8'))
                                    file.write(b'\x00')
        tmp_cachedir.cleanup()

        # Extra file cargo creates to keep track of files to fetch
        runfetchcmd("touch %s/.last-updated" % destdir, d)

        return True
