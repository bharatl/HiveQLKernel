import json
import logging
import traceback

from ipykernel.kernelbase import Kernel
from sqlalchemy.exc import OperationalError

from .constants import __version__, KERNEL_NAME

from sqlalchemy import *
import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class KernelSyntaxError(Exception):
    pass


error_con_not_created = """Connection not initialized!
Please specify your pyHive configuration like this :

-------------
$$ url=hive://<kerberos-username>@<hive-host>:<hive-port>/<db-name>
$$ connect_args={"auth": "KERBEROS","kerberos_service_name": "hive"}
$$ pool_size=5
$$ max_overflow=10

YOUR SQL REQUEST HERE IF ANY
-------------

-> if you want to update the current connection, just type it again with another configuration
-> $$ are mandatory characters that specify that this line is a configuration for this kernel

Other parameters are available such as :

$$ default_limit=50 # -> without this parameter, default_limit is set to 20
$$ display_mode=be # -> this will display a table with the beginning (b) and end (e) of the SQL response (options are: b, e and be)

"""


class ConnectionNotCreated(Exception):
    def __init__(self):
        Exception.__init__(self, error_con_not_created)


class HiveQLKernel(Kernel):
    implementation = KERNEL_NAME
    implementation_version = __version__
    banner = 'HiveQL REPL'
    language_info = {
        'name': 'hive',
        'codemirror_mode': 'python',
        'pygments_lexer': 'sql',
        'mimetype': 'text/plain',
        'file_extension': '.hiveql',
    }
    last_conn = None
    params = {
        "default_limit": 20,
        "display_mode": "be"
    }


    def send_exception(self, e):
        if type(e) in [ConnectionNotCreated]:
            tb = ""
        else:
            tb = "\n" + traceback.format_exc()
        return self.send_error(str(e) + tb)

    def send_error(self, contents):
        self.send_response(self.iopub_socket, 'stream', {
            'name': 'stderr',
            'text': str(contents)
        })
        return {
            'status': 'error',
            'execution_count': self.execution_count,
            'payload': [],
            'user_expressions': {}
        }

    def send_info(self, contents):
        self.send_response(self.iopub_socket, 'stream', {
            'name': 'stdout',
            'text': str(contents)
        })

    def create_conn(self, url, **kwargs):
        self.send_info("create_engine('" + url + "', " + ', '.join(
            [str(k) + '=' + (str(v) if type(v) == str else json.dumps(v)) for k, v in kwargs.items()]) + ")\n")
        self.last_conn = create_engine(url, **kwargs)
        self.last_conn.connect()
        self.send_info("Connection established to database!\n")

    def reconfigure(self, params):
        if 'default_limit' in params:
            try:
                self.params['default_limit'] = int(params['default_limit'])
                self.send_info("Set display limit to {}".format(self.params['default_limit']))
            except ValueError as e:
                self.send_exception(e)
        if 'display_mode' in params:
            v = params['display_mode']
            if type(v) == str and v in ['b', 'e', 'be']:
                self.params['display_mode'] = v
            else:
                self.send_error("Invalid display_mode, options are b, e and be.")

    def parse_code(self, code):
        req = code.strip()

        headers = {}
        sql_req = ""
        beginning = True
        for l in req.split('\n'):
            l = l.strip()
            if l.startswith("$$"):
                if beginning:
                    k, v = l.replace("$", "").split("=")
                    k, v = k.strip(), v.strip()
                    if v.startswith('{'):
                        v = json.loads(v)
                    else:
                        try:
                            v = int(v)
                        except ValueError:
                            pass
                    headers[k] = v
                else:
                    raise KernelSyntaxError("Headers starting with %% must be at the beginning of your request.")
            else:
                beginning = False
                sql_req += ' ' + l

        sql_req = sql_req.strip()
        if sql_req.endswith(';'):
            sql_req = sql_req[:-1]

        a = ['default_limit', 'display_mode']
        params, pyhiveconf = {k: v for k, v in headers.items() if k in a}, {k: v for k, v in headers.items() if k not in a}

        self.reconfigure(params)

        return pyhiveconf, sql_req

    def do_execute(self, code, silent, store_history=True, user_expressions=None, allow_stdin=False):
        try:
            pyhiveconf, sql_req = self.parse_code(code)

            if 'url' in pyhiveconf:
                self.create_conn(**pyhiveconf)

            if self.last_conn is None:
                raise ConnectionNotCreated()

            # If code empty
            if not sql_req:
                return {
                    'status': 'ok',
                    'execution_count': self.execution_count,
                    'payload': [],
                    'user_expressions': {}
                }

            if self.params['default_limit'] > 0 and (sql_req.startswith('select') or sql_req.startswith('with')):
                sql_req = "select * from ({}) hzykwyxnbv limit {}".format(sql_req, self.params['default_limit'])
            logger.info("Running the following HiveQL query: {}".format(sql_req))
            # todo
            # if self.params['display_mode'] == 'b':
            # if self.params['display_mode'] == 'e':
            # if self.params['display_mode'] == 'be':

            html = pd.read_sql(sql_req, self.last_conn).to_html()
        except OperationalError as oe:
            return self.send_error(oe)
        except Exception as e:
            return self.send_exception(e)

        # msg_types = https://jupyter-client.readthedocs.io/en/latest/messaging.html?highlight=stream#messages-on-the-iopub-pub-sub-channel
        self.send_response(self.iopub_socket, 'execute_result', {
            "execution_count": self.execution_count,
            'data': {
                "text/html": html,
            },
            "metadata": {
                "image/png": {
                    "width": 640,
                    "height": 480,
                },
            }
        })

        return {
            'status': 'ok',
            'execution_count': self.execution_count,
            'payload': [],
            'user_expressions': {}
        }