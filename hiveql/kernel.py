import json
import logging
import traceback
import re
import os.path

from ipykernel.kernelbase import Kernel
from sqlalchemy.exc import OperationalError, ResourceClosedError

from .constants import __version__, KERNEL_NAME, CONFIG_FILE

from sqlalchemy import *
import pandas as pd
from .tool_sql import *

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
    language = "hiveql"
    language_info = {
        'name': 'hive',
        'codemirror_mode': "sql",
        'pygments_lexer': 'postgresql',
        'mimetype': 'text/x-hive',
        'file_extension': '.hiveql',
    }
    last_conn = None
    params = {
        "default_limit": 20,
        "display_mode": "be"
    }
    conf = None
    conf_file = os.path.expanduser(CONFIG_FILE)
    if os.path.isfile(conf_file):
        with open(conf_file, mode='r') as file_hanlde:
            conf = json.load(file_hanlde)

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
                self.send_info("Set display limit to {}\n".format(self.params['default_limit']))
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
                if not l.startswith("--"):
                    sql_req += ' ' + l

        if self.last_conn is None and not headers and self.conf is not None:
            headers = self.conf  # if cells doesn't contain $$ and connection is None, overriding headers with conf data

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
            sql_req = sql_remove_comment(sql_req)
            sql_validate(sql_req)
            sql_str = sql_rewrite(sql_req, self.params['default_limit'])
            logger.info("Running the following HiveQL query: {}".format(sql_req))

            pd.set_option('display.max_colwidth', -1)
            if sql_is_create(sql_req):
                self.last_conn.execute(sql_str)
                self.send_info("Table created!")
                return { 'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {} }
            if sql_is_drop(sql_req):
                self.last_conn.execute(sql_str)
                self.send_info("Table dropped!")
                return { 'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {} }
            if sql_is_use(sql_req):
                self.last_conn.execute(sql_str)
                self.send_info("Database changed!")
                return { 'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {} }
            if sql_is_set_variable(sql_req):
                for s in sql_req.split(";"):
                    self.last_conn.execute(s.strip())
                self.send_info("variables set!")
                return {'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {}}

            df = pd.read_sql(sql_str, self.last_conn)
            if sql_is_show(sql_req):
                if sql_is_show_tables(sql_req):
                    html = df_to_html(df[df.tab_name.str.contains( extract_show_pattern(sql_req))])
                if sql_is_show_databases(sql_req):
                    html = df_to_html(df[df.database_name.str.contains( extract_show_pattern(sql_req))])
            else:
                html = df_to_html(df)

        except OperationalError as oe:
            return self.send_error(oe)
        except ResourceClosedError as rce:
                return self.send_error(rce)
        except MultipleQueriesError as e:
            return self.send_error("Only one query per cell!")
        except NotAllowedQueriesError as e:
            return self.send_error("only 'select', 'with', 'set property=value', 'create table x.y stored as orc' 'drop table', 'use database', 'show databases', 'show tables', 'describe myTable' statements are allowed")
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
def df_to_html(df):
    #for column in df:
    #    if df[column].dtype == 'object':
    #        df[column] =  df[column].apply(lambda x: x.replace("\n","<br>"))
    return df.fillna('NULL').astype(str).to_html(notebook=True)

