# -*-python-*-
#
# Copyright (C) 1999-2025 The ViewCVS Group. All Rights Reserved.
#
# By using this file, you agree to the terms and conditions set forth in
# the LICENSE.html file which can be found at the top level of the ViewVC
# distribution or at http://viewvc.org/license-1.html.
#
# For more information, visit http://viewvc.org/
#
# -----------------------------------------------------------------------

"Version Control lib driver for Subversion repositories"

import os
import os.path
import re
import urllib.parse
from vclib import _getfspath, os_listdir

_re_url = re.compile(r"^(http|https|file|svn|svn\+[^:]+)://")


def _strpath(pathb):
    """Convert a path represented in UTF-8 encoded bytes string into str path.

    PATHB should be a path returned by Subversion SWIG Python bindings API,
    which represented in a UTF-8 encoded Unicode string in bytes object.
    The return value is a Unicode path in str."""

    return None if pathb is None else pathb.decode("utf-8", "surrogateescape")


def _canonicalize_path(path):
    import svn.core

    return _strpath(svn.core.svn_path_canonicalize(path))


def canonicalize_rootpath(rootpath):
    # Try to canonicalize the rootpath using Subversion semantics.
    rootpath = _canonicalize_path(rootpath)

    # ViewVC's support for local repositories is more complete and more
    # performant than its support for remote ones, so if we're on a
    # Unix-y system and we have a file:/// URL, convert it to a local
    # path instead.
    if os.name == "posix":
        rootpath_lower = rootpath.lower()
        if rootpath_lower in ["file://localhost", "file://localhost/", "file://", "file:///"]:
            return "/"
        if rootpath_lower.startswith("file://localhost/"):
            rootpath = os.path.normpath(urllib.parse.unquote(rootpath[16:]))
        elif rootpath_lower.startswith("file:///"):
            rootpath = os.path.normpath(urllib.parse.unquote(rootpath[7:]))

    # Ensure that we have an absolute path (or URL), and return.
    if not re.search(_re_url, rootpath):
        assert os.path.isabs(rootpath)
    return rootpath


def expand_root_parent(parent_path, path_encoding):
    roots = {}
    if re.search(_re_url, parent_path):
        pass
    else:
        # Any subdirectories of PARENT_PATH which themselves have a child
        # "format" are returned as roots.
        assert os.path.isabs(parent_path)
        subpaths = os_listdir(parent_path, path_encoding)
        for rootname in subpaths:
            rootpath = os.path.join(parent_path, rootname)
            if os.path.exists(_getfspath(os.path.join(rootpath, "format"),
                                         path_encoding)):
                roots[rootname] = canonicalize_rootpath(rootpath)
    return roots


def find_root_in_parent(parent_path, rootname, path_encoding):
    """Search PARENT_PATH for a root named ROOTNAME, returning the
    canonicalized ROOTPATH of the root if found; return None if no such
    root is found."""

    if not re.search(_re_url, parent_path):
        assert os.path.isabs(parent_path)
        rootpath = os.path.join(parent_path, rootname)
        format_path = os.path.join(rootpath, "format")
        if os.path.exists(_getfspath(format_path, path_encoding)):
            return canonicalize_rootpath(rootpath)
    return None


def SubversionRepository(name, rootpath, authorizer, utilities, config_dir,
                         content_encoding, path_encoding):
    rootpath = canonicalize_rootpath(rootpath)
    if re.search(_re_url, rootpath):
        from . import svn_ra

        return svn_ra.RemoteSubversionRepository(
            name, rootpath, authorizer, utilities, config_dir, content_encoding
        )
    else:
        from . import svn_repos

        return svn_repos.LocalSubversionRepository(
            name, rootpath, authorizer, utilities, config_dir,
            content_encoding, path_encoding
        )
