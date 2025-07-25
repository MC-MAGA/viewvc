#!/usr/bin/env python3
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
#
# This program originally written by Peter Funk <pf@artcom-gmbh.de>, with
# contributions by Ka-Ping Yee.
#
# -----------------------------------------------------------------------

import sys
import os
import os.path
import posixpath
import string
import socket
import select
import base64
from urllib.parse import unquote as _unquote
import http.server as _http_server


#########################################################################
#
# INSTALL-TIME CONFIGURATION
#
# These values will be set during the installation process. During
# development, they will remain None.
#

LIBRARY_DIR = None
CONF_PATHNAME = None


#########################################################################

if LIBRARY_DIR:
    sys.path.insert(0, LIBRARY_DIR)
else:
    sys.path.insert(0, os.path.abspath(os.path.join(sys.argv[0], "../../lib")))

import sapi
import viewvc

# We use passlib (https://pypi.org/project/passlib/) for htpasswd support.
has_passlib = True
try:
    from passlib.apache import HtpasswdFile
except ImportError:
    has_passlib = False


class Options:
    port = 49152  # default TCP/IP port used for the server
    repositories = {}  # use default repositories specified in config
    host = sys.platform == "mac" and "127.0.0.1" or "localhost"
    script_alias = "viewvc"
    config_file = None
    htpasswd_file = None


class StandaloneServer(sapi.CgiServer):
    """Custom sapi interface that uses a BaseHTTPRequestHandler HANDLER
    to generate output."""

    def __init__(self, handler):
        sapi.Server.__init__(self)
        self._headers = []
        self._handler = handler
        self._out_fp = handler.wfile
        self._iis = False
        global server
        server = self

    def start_response(self, content_type="text/html; charset=UTF-8", status=None):
        sapi.Server.start_response(self, content_type, status)
        if status is None:
            statusCode = 200
            statusText = "OK"
        else:
            p = status.find(" ")
            if p < 0:
                statusCode = int(status)
                statusText = ""
            else:
                statusCode = int(status[:p])
                statusText = status[(p + 1) :]
        self._handler.send_response(statusCode, statusText)
        self._handler.send_header("Content-type", content_type)
        for name, value in self._headers:
            self._handler.send_header(name, value)
        self._handler.end_headers()

    def write_text(self, s):
        self.write(self, s.encode("utf-8", "surrogateesape"))

    def redirect(self, url):
        self.add_header("Location", url)
        self.start_response(status="301 Moved")
        self.write_text(sapi.redirect_notice(url))

    def write(self, s):
        self._out_fp.write(s)

    def flush(self):
        self._out_fp.flush()

    def file(self):
        return self._out_fp


class NotViewVCLocationException(Exception):
    """The request location was not aimed at ViewVC."""

    pass


class AuthenticationException(Exception):
    """Authentication requirements have not been met."""

    pass


class ViewVCHTTPRequestHandler(_http_server.BaseHTTPRequestHandler):
    """Custom HTTP request handler for ViewVC."""

    def do_GET(self):
        """Serve a GET request."""
        self.handle_request("GET")

    def do_POST(self):
        """Serve a POST request."""
        self.handle_request("POST")

    def handle_request(self, method):
        """Handle a request of type METHOD."""
        try:
            self.run_viewvc()
        except NotViewVCLocationException:
            # If the request was aimed at the server root, but there's a
            # non-empty script_alias, automatically redirect to the
            # script_alias.  Otherwise, just return a 404 and shrug.
            if (not self.path or self.path == "/") and options.script_alias:
                new_url = self.server.url + options.script_alias + "/"
                self.send_response(301, "Moved Permanently")
                self.send_header("Content-type", "text/html")
                self.send_header("Location", new_url)
                self.end_headers()
                self.wfile.write(
                    (
                        """<html>
<head>
<meta http-equiv="refresh" content="10; url=%s" />
<title>Moved Temporarily</title>
</head>
<body>
<h1>Redirecting to ViewVC</h1>
<p>You will be automatically redirected to <a href="%s">ViewVC</a>.
   If this doesn't work, please click on the link above.</p>
</body>
</html>
"""
                        % (new_url, new_url)
                    ).encode("utf-8")
                )
            else:
                self.send_error(404)
        except IOError:  # ignore IOError: [Errno 32] Broken pipe
            pass
        except AuthenticationException:
            self.send_response(401, "Unauthorized")
            self.send_header("WWW-Authenticate", 'Basic realm="ViewVC"')
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"""<html>
<head>
<title>Authentication failed</title>
</head>
<body>
<h1>Authentication failed</h1>
<p>Authentication has failed.  Please retry with the correct username
   and password.</p>
</body>
</html>"""
            )

    def is_viewvc(self, path):
        """Check whether self.path is, or is a child of, the ScriptAlias"""
        if not path.startswith("/"):
            return False
        if not options.script_alias:
            return True
        if path == "/" + options.script_alias:
            return True
        if path.startswith("/" + options.script_alias + "/"):
            return True
        return False

    def validate_password(self, htpasswd_file, username, password):
        """Compare USERNAME and PASSWORD against HTPASSWD_FILE."""
        try:
            ht = HtpasswdFile(htpasswd_file)
            return ht.check_password(username, password)
        except Exception:
            pass
        return False

    def run_viewvc(self):
        """Run ViewVC to field a single request."""

        # NOTE: Much of this is adapter from Python's standard library
        # module CGIHTTPServer.

        i = self.path.rfind("?")
        if i >= 0:
            path = _unquote(self.path[:i], "utf-8", "surrogateescape")
            query = self.path[(i + 1) :]
        else:
            path = _unquote(self.path)
            query = ""
        # normalize path
        path = posixpath.normpath(path) + ("/" if path[-1] == "/" else "")

        # Is this request even aimed at ViewVC?  If not, complain.
        if not self.is_viewvc(path):
            raise NotViewVCLocationException()

        # If htpasswd authentication is enabled, try to authenticate the user.
        self.username = None
        if options.htpasswd_file:
            authn = self.headers.get("authorization")
            if not authn:
                raise AuthenticationException()
            try:
                kind, data = authn.split(" ", 1)
                if kind == "Basic":
                    data = base64.b64decode(data).decode("ascii")
                    username, password = data.split(":", 1)
            except Exception:
                raise AuthenticationException()
            if not self.validate_password(options.htpasswd_file, username, password):
                raise AuthenticationException()
            self.username = username

        # Setup the environment in preparation of executing ViewVC's core code.
        env = os.environ

        scriptname = options.script_alias and "/" + options.script_alias or ""

        rest = path[len(scriptname) :]

        # Since we're going to modify the env in the parent, provide empty
        # values to override previously set values
        for k in env.keys():
            if k[:5] == "HTTP_":
                del env[k]
        for k in (
            "QUERY_STRING",
            "REMOTE_HOST",
            "CONTENT_LENGTH",
            "HTTP_USER_AGENT",
            "HTTP_COOKIE",
        ):
            if k in env:
                env[k] = ""

        # XXX Much of the following could be prepared ahead of time!
        env["SERVER_SOFTWARE"] = self.version_string()
        env["SERVER_NAME"] = self.server.server_name
        env["GATEWAY_INTERFACE"] = "CGI/1.1"
        env["SERVER_PROTOCOL"] = self.protocol_version
        env["SERVER_PORT"] = str(self.server.server_port)
        env["REQUEST_METHOD"] = self.command
        env["PATH_INFO"] = rest
        env["SCRIPT_NAME"] = scriptname
        if query:
            env["QUERY_STRING"] = query
        env["HTTP_HOST"] = self.server.address[0]
        host = self.address_string()
        if host != self.client_address[0]:
            env["REMOTE_HOST"] = host
        env["REMOTE_ADDR"] = self.client_address[0]
        if self.username:
            env["REMOTE_USER"] = self.username
        env["CONTENT_TYPE"] = self.headers.get_content_type()
        length = self.headers.get("content-length", None)
        if length:
            env["CONTENT_LENGTH"] = length
        accept = []
        for line in self.headers.getallmatchingheaders("accept"):
            if line[:1] in string.whitespace:
                accept.append(line.strip())
            else:
                accept = accept + line[7:].split(",")
        env["HTTP_ACCEPT"] = ",".join(accept)
        ua = self.headers.get("user-agent", None)
        if ua:
            env["HTTP_USER_AGENT"] = ua
        modified = self.headers.get("if-modified-since", None)
        if modified:
            env["HTTP_IF_MODIFIED_SINCE"] = modified
        etag = self.headers.get("if-none-match", None)
        if etag:
            env["HTTP_IF_NONE_MATCH"] = etag
        # AUTH_TYPE
        # REMOTE_IDENT
        # XXX Other HTTP_* headers

        # make a one time cfg for a single request
        ot_cfg = cfg.copy()
        try:
            try:
                viewvc.main(StandaloneServer(self), ot_cfg)
            finally:
                if not self.wfile.closed:
                    self.wfile.flush()
        except SystemExit as status:
            self.log_error("ViewVC exit status %s", str(status))
        else:
            self.log_error("ViewVC exited ok")


class ViewVCHTTPServer(_http_server.HTTPServer):
    """Customized HTTP server for ViewVC."""

    def __init__(self, host, port, callback):
        self.address = (host, port)
        self.url = "http://%s:%d/" % (host, port)
        self.callback = callback
        _http_server.HTTPServer.__init__(self, self.address, self.handler)

    def serve_until_quit(self):
        self.quit = 0
        while not self.quit:
            rd, wr, ex = select.select([self.socket.fileno()], [], [], 1)
            if rd:
                self.handle_request()

    def server_activate(self):
        _http_server.HTTPServer.server_activate(self)
        if self.callback:
            self.callback(self)

    def server_bind(self):
        # set SO_REUSEADDR (if available on this platform)
        if hasattr(socket, "SOL_SOCKET") and hasattr(socket, "SO_REUSEADDR"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _http_server.HTTPServer.server_bind(self)

    def handle_error(self, request, client_address):
        """Handle an error gracefully. use stderr instead of stdout
        to avoid double fault.
        """
        sys.stderr.write("-" * 40 + "\n")
        sys.stderr.write(
            "Exception happened during processing of request from %s\n" % str(client_address)
        )
        import traceback

        traceback.print_exc()
        sys.stderr.write("-" * 40 + "\n")


def serve(host, port, callback=None):
    """Start an HTTP server for HOST on PORT.  Call CALLBACK function
    when the server is ready to serve."""

    ViewVCHTTPServer.handler = ViewVCHTTPRequestHandler

    try:
        # XXX Move this code out of this function.
        # Early loading of configuration here.  Used to allow tinkering
        # with some configuration settings:
        handle_config(options.config_file)
        if options.repositories:
            cfg.general.default_root = "Development"
            for repo_name in options.repositories.keys():
                repo_path = os.path.normpath(options.repositories[repo_name])
                if os.path.exists(os.path.join(repo_path, "CVSROOT", "config")):
                    cfg.general.cvs_roots[repo_name] = repo_path
                elif os.path.exists(os.path.join(repo_path, "format")):
                    cfg.general.svn_roots[repo_name] = repo_path
        elif "Development" in cfg.general.cvs_roots and not os.path.isdir(
            cfg.general.cvs_roots["Development"]
        ):
            sys.stderr.write("*** No repository found. Please use the -r option.\n")
            sys.stderr.write("   Use --help for more info.\n")
            raise KeyboardInterrupt  # Hack!
        os.close(0)  # To avoid problems with shell job control

        # always use default docroot location
        cfg.options.docroot = None

        ViewVCHTTPServer(host, port, callback).serve_until_quit()
    except (KeyboardInterrupt, select.error):
        pass
    print("server stopped", flush=True)


def handle_config(config_file):
    global cfg
    cfg = viewvc.load_config(config_file or CONF_PATHNAME)


def usage():
    clean_options = Options()
    cmd = os.path.basename(sys.argv[0])
    port = clean_options.port
    host = clean_options.host
    script_alias = clean_options.script_alias
    sys.stderr.write(
        """Usage: %(cmd)s [OPTIONS]

Run a simple, standalone HTTP server configured to serve up ViewVC requests.

Options:

  --config-file=FILE (-c)    Read configuration options from FILE.  If not
                             specified, ViewVC will look for a configuration
                             file in its installation tree, falling back to
                             built-in default values.

  --daemon (-d)              Background the server process.

  --help                     Show this usage message and exit.

  --host=HOSTNAME (-h)       Listen on HOSTNAME.  Required for access from a
                             remote machine.  [default: %(host)s]

  --htpasswd-file=FILE       Authenticate incoming requests, validating against
                             against FILE, which is an Apache HTTP Server
                             htpasswd file.

  --port=PORT (-p)           Listen on PORT.  [default: %(port)d]

  --repository=PATH (-r)     Serve the Subversion or CVS repository located
                             at PATH.  This option may be used more than once.

  --script-alias=PATH (-s)   Use PATH as the virtual script location (similar
                             to Apache HTTP Server's ScriptAlias directive).
                             For example, "--script-alias=repo/view" will serve
                             ViewVC at "http://HOSTNAME:PORT/repo/view".
                             [default: %(script_alias)s]
"""
        % locals()
    )
    sys.exit(0)


def badusage(errstr):
    cmd = os.path.basename(sys.argv[0])
    sys.stderr.write(
        "ERROR: %s\n\nTry '%s --help' for detailed usage information.\n" % (errstr, cmd)
    )
    sys.exit(1)


def main(argv):
    """Command-line interface (looks at argv to decide what to do)."""
    import getopt

    short_opts = "".join(
        [
            "c:",
            "d",
            "h:",
            "p:",
            "r:",
            "s:",
        ]
    )
    long_opts = [
        "daemon",
        "config-file=",
        "help",
        "host=",
        "htpasswd-file=",
        "port=",
        "repository=",
        "script-alias=",
    ]

    opt_daemon = False
    opt_host = None
    opt_port = None
    opt_htpasswd_file = None
    opt_config_file = None
    opt_script_alias = None
    opt_repositories = []

    # Parse command-line options.
    try:
        opts, args = getopt.getopt(argv[1:], short_opts, long_opts)
        for opt, val in opts:
            if opt in ["--help"]:
                usage()
            elif opt in ["-r", "--repository"]:  # may be used more than once
                opt_repositories.append(val)
            elif opt in ["-d", "--daemon"]:
                opt_daemon = 1
            elif opt in ["-p", "--port"]:
                opt_port = val
            elif opt in ["-h", "--host"]:
                opt_host = val
            elif opt in ["-s", "--script-alias"]:
                opt_script_alias = val
            elif opt in ["-c", "--config-file"]:
                opt_config_file = val
            elif opt in ["--htpasswd-file"]:
                opt_htpasswd_file = val
    except getopt.error as err:
        badusage(str(err))

    # Validate options that need validating.
    class BadUsage(Exception):
        pass

    try:
        if opt_port is not None:
            try:
                options.port = int(opt_port)
            except ValueError:
                raise BadUsage("Port '%s' is not a valid port number" % (opt_port))
            if not options.port:
                raise BadUsage("You must supply a valid port.")
        if opt_htpasswd_file is not None:
            if not os.path.isfile(opt_htpasswd_file):
                raise BadUsage(
                    "'%s' does not appear to be a valid htpasswd file." % (opt_htpasswd_file)
                )
            if not has_passlib:
                raise BadUsage(
                    "Unable to locate suitable `passlib' module for use "
                    "with --htpasswd-file option.  This module is not a "
                    "standard library module, but it can be found in "
                    "PyPI (https://pypi.org/project/passlib/)."
                )
            options.htpasswd_file = opt_htpasswd_file
        if opt_config_file is not None:
            if not os.path.isfile(opt_config_file):
                raise BadUsage(
                    "'%s' does not appear to be a valid configuration file." % (opt_config_file)
                )
            options.config_file = opt_config_file
        if opt_host is not None:
            options.host = opt_host
        if opt_script_alias is not None:
            options.script_alias = "/".join(filter(None, opt_script_alias.split("/")))
        for repository in opt_repositories:
            if "Development" not in options.repositories:
                rootname = "Development"
            else:
                rootname = "Repository%d" % (len(options.repositories.keys()) + 1)
            options.repositories[rootname] = repository
    except BadUsage as err:
        badusage(str(err))

    # Fork if we're in daemon mode.
    if opt_daemon:
        pid = os.fork()
        if pid != 0:
            sys.exit()

    # Finaly, start the server.
    def ready(server):
        print(f"server ready at {server.url}{options.script_alias}", flush=True)

    serve(options.host, options.port, ready)


if __name__ == "__main__":
    options = Options()
    main(sys.argv)
