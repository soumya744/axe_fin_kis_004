########################################################
consumer_name = 'api_oneapi_consumer'
import os, sys
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(Path(__file__).parents[1]))
from pathlib import Path

import logging
logger = logging.getLogger(__name__)

from datetime import datetime
import json
import threading
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

########################################################

# set timeout duration (seconds)
timeout_duration = 25

# -------------------------------------------------------
# OneAPI Base URLs
# -------------------------------------------------------
ONEAPI_BASE_URLS = {
    'dev': 'https://io-api-lab.aexp.com',
    'qa':  'https://io-api-lab.aexp.com',    # TODO: update when QA URL is provided
    'prod': 'https://io-api-lab.aexp.com',   # TODO: update when PROD URL is provided
}

# AuthBlue token endpoint
AUTHBLUE_TOKEN_URL = 'https://authbluetokens.aexp.com/v1/app2app/tokens'

# OneAPI service account (password must be set via env var ONEAPI_SERVICE_PASSWORD)
ONEAPI_SERVICE_ID = os.environ.get('ONEAPI_SERVICE_ID', 'svc.oneapi-e2')


class ConsumerException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


# =====================================================================
# Base Action Class
# =====================================================================
class OneAPIAction:
    """Base class for all OneAPI actions.

    Key differences from IcingaAction:
    - Auth: AuthBlue App2App Bearer token  (instead of Icinga NTLM/Basic)
    - Base URL: https://io-api-lab.aexp.com  (instead of Icinga Director URLs)
    - No login session required; every call uses a fresh Bearer token
    """

    def __init__(self, data: dict):
        self.data = data
        self.action = data.get('action', '')
        self.headers = {
            'Content-Type': 'application/json',
            'Accept':        'application/json',
        }
        self.errors = []
        self.auth_token = None
        self.base_url = ''
        self.method = 'POST'
        self.path = ''
        self.body = {}
        self.required_keys = []

        self._resolve_base_url()
        self._acquire_auth_token()

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------
    def _resolve_base_url(self):
        """Pick the right OneAPI base URL.

        Priority:
        1. Explicit 'api_url' key in data  (backwards-compatible override)
        2. Environment variable ONEAPI_ENV  ('dev' | 'qa' | 'prod')
        3. Fallback to 'dev'
        """
        if self.data.get('api_url'):
            self.base_url = self.data['api_url']
        else:
            env = os.environ.get('ONEAPI_ENV', 'dev').lower()
            self.base_url = ONEAPI_BASE_URLS.get(env, ONEAPI_BASE_URLS['dev'])
        logger.info('OneAPI base_url: {}'.format(self.base_url))

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _acquire_auth_token(self):
        """Obtain a Bearer token.

        Priority:
        1. --token CLI arg  (data['token'])
        2. ONEAPI_BEARER_TOKEN env var  → use directly, skip AuthBlue
        3. ONEAPI_BASIC_AUTH env var    → pre-encoded Basic auth header value
        4. ONEAPI_SERVICE_PASSWORD      → build Basic auth from service ID + password

        AuthBlue curl equivalent (from reference):
            curl --location --request POST
              'https://authbluetokens.aexp.com/v1/app2app/tokens'
              --header 'Content-Type: application/json'
              --header 'Authorization: Basic <base64-encoded-credentials>'
              --data-raw '{
                "scope": {
                  "attributes": ["givenname", "sn", "department"],
                  "groups": ["infraobserve-test"]
                }
              }'
        """
        import base64

        # 1 & 2 — pre-fetched Bearer token (skip AuthBlue entirely)
        direct_token = self.data.get('token', '') or os.environ.get('ONEAPI_BEARER_TOKEN', '')
        if direct_token:
            self.auth_token = direct_token
            self.headers['Authorization'] = f'Bearer {self.auth_token}'
            logger.info('Using pre-set Bearer token.')
            return

        # 3 — pre-encoded Basic auth string  e.g. c3ZjLm...
        basic_auth = os.environ.get('ONEAPI_BASIC_AUTH', '')
        if not basic_auth:
            # 4 — build Basic auth from service ID + password
            service_password = os.environ.get('ONEAPI_SERVICE_PASSWORD', '')
            if not service_password:
                logger.warning(
                    'No token source found. Set --token, ONEAPI_BEARER_TOKEN, '
                    'ONEAPI_BASIC_AUTH, or ONEAPI_SERVICE_PASSWORD.'
                )
            raw = f'{ONEAPI_SERVICE_ID}:{service_password}'
            basic_auth = base64.b64encode(raw.encode()).decode()

        # Correct AuthBlue request body (matches curl from reference)
        auth_body = {
            "scope": {
                "attributes": ["givenname", "sn", "department"],
                "groups": ["infraobserve-test"]
            }
        }
        auth_headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Basic {basic_auth}',
        }

        try:
            response = requests.post(
                AUTHBLUE_TOKEN_URL,
                headers=auth_headers,
                json=auth_body,
                verify=False,
                timeout=timeout_duration
            )
            if response.ok:
                token_data = response.json()
                self.auth_token = (
                    token_data.get('access_token')
                    or token_data.get('token')
                    or token_data.get('id_token')
                )
                self.headers['Authorization'] = f'Bearer {self.auth_token}'
                logger.info('Successfully obtained AuthBlue Bearer token.')
            else:
                msg = f'AuthBlue token request failed: {response.status_code} {response.text}'
                logger.error(msg)
                raise ConsumerException(msg)

        except requests.ConnectionError as e:
            msg = f'Unable to connect to AuthBlue: {e}'
            logger.error(msg)
            raise ConsumerException(msg)

    # ------------------------------------------------------------------
    # Endpoint URL
    # ------------------------------------------------------------------
    @property
    def endpoint_url(self):
        return str(self.base_url).rstrip('/') + self.path

    # ------------------------------------------------------------------
    # Required field helper
    # ------------------------------------------------------------------
    @property
    def required(self):
        return {k: self.data[k] for k in self.required_keys if k in self.data}

    # ------------------------------------------------------------------
    # HTTP request
    # ------------------------------------------------------------------
    def oneapi_request(self):
        """Execute the HTTP call and return a normalised response dict."""
        logger.info('{s} HTTP VERB:  {v}'.format(s=threading.current_thread(), v=self.method))
        logger.info('{s} ENDPOINT:   {u}'.format(s=threading.current_thread(), u=self.endpoint_url))
        logger.info('{s} HEADERS:    {h}'.format(s=threading.current_thread(), h=self.headers))
        logger.info('{s} BODY:       {b}'.format(s=threading.current_thread(), b=json.dumps(self.body)))

        try:
            if self.method == 'GET':
                response = requests.get(
                    self.endpoint_url,
                    headers=self.headers,
                    verify=False,
                    timeout=timeout_duration
                )
            elif self.method == 'DELETE':
                response = requests.delete(
                    self.endpoint_url,
                    headers=self.headers,
                    json=self.body,
                    verify=False,
                    timeout=timeout_duration
                )
            else:
                response = requests.post(
                    self.endpoint_url,
                    headers=self.headers,
                    json=self.body,
                    verify=False,
                    timeout=timeout_duration
                )

            response_json = {"message": "", "reason": "", "status": ""}
            try:
                response_json = response.json()
                logger.info("JSON Response: " + str(response_json))
            except (json.JSONDecodeError, ValueError):
                logger.info("No JSON response returned")
                response_json["message"] = response.text
                response_json["reason"] = response.reason
                response_json["status"] = "completed" if response.ok else "failed"

            if response.ok:
                logger.info('API call successful.')
                logger.info("Response JSON: " + str(response_json))
            else:
                logger.error('API call failed.')
                logger.error("Status code: {}".format(response.status_code))
                logger.error("Response JSON: " + str(response_json))

            return response_json

        except Exception as e:
            msg = str(getattr(e, 'message', e))
            logger.error('Unexpected error: ' + msg)
            raise ConsumerException(msg)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def output(self):
        self.data['jl_response'] = self.oneapi_request()
        self.data['jd_response'] = json.dumps(self.data['jl_response'], indent=4)
        return self.data


# =====================================================================
# Silence / Blackout Actions
# (CAUT-1578 – Schedule Downtime, CAUT-1579 – Remove Downtime)
# =====================================================================

def _to_iso(value):
    """Convert a timestamp or date-string to ISO 8601 format (YYYY-MM-DDTHH:MM:SS).

    Icinga used Unix integer timestamps.
    OneAPI expects ISO strings e.g. '2026-06-19T03:20:00'.
    """
    if not value:
        return ''
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return datetime.utcfromtimestamp(int(value)).strftime('%Y-%m-%dT%H:%M:%S')
    # Already a string – try common formats
    for fmt in ('%d/%m/%Y %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(str(value), fmt).strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            continue
    return str(value)   # return as-is if we can't parse


class ScheduleSilence(OneAPIAction):
    """Schedule a silence/blackout via OneAPI.

    Replaces:  ScheduleDowntime  →  POST /icingaweb2/monitoring/host/schedule-downtime
    New call:  POST https://io-api-lab.aexp.com/silence

    Required keys: host_name, start, end, comment, requestor
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'scheduleSilence'
        self.method = 'POST'
        self.path = '/silence'
        self.required_keys = ['host_name', 'start', 'end', 'comment', 'requestor']

        self.body = {
            "comment":       data.get('comment', ''),
            "end_time_mst":  _to_iso(data.get('end', '')),
            "host_names":    [data['host_name']] if data.get('host_name') else [],
            "start_time_mst": _to_iso(data.get('start', '')),
        }


class GetSilenceStatus(OneAPIAction):
    """Retrieve the status of a previously submitted silence job.

    New call:  GET https://io-api-lab.aexp.com/silence/<job_id>

    Required keys: job_id
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'getSilenceStatus'
        self.method = 'GET'
        job_id = data.get('job_id', '')
        self.path = f'/silence/{job_id}'
        self.required_keys = ['job_id']
        self.body = {}


class RemoveSilence(OneAPIAction):
    """Remove an active silence/blackout via OneAPI.

    Replaces:  RemoveDowntime  →  POST /icingaweb2/monitoring/downtimes/delete-all
    New call:  DELETE https://io-api-lab.aexp.com/silence/<job_id>
               (or POST /silence/remove – verify from https://io-api-lab.aexp.com/docs)

    Required keys: host_name, requestor
    Optional key:  job_id  (if known)
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'removeSilence'
        self.required_keys = ['host_name', 'requestor']

        job_id = data.get('job_id', '')
        if job_id:
            # Preferred: target a specific silence by job_id
            self.method = 'DELETE'
            self.path = f'/silence/{job_id}'
            self.body = {}
        else:
            # Fallback: remove by host name
            # TODO: confirm exact endpoint from https://io-api-lab.aexp.com/docs
            self.method = 'POST'
            self.path = '/silence/remove'
            self.body = {
                "host_names": [data.get('host_name', '')],
                "comment":    data.get('comment', ''),
            }


# =====================================================================
# Host / Object Management Actions
# (CAUT-1576 – Add objects, CAUT-1577 – Disable/Enable objects)
#
# NOTE: The exact OneAPI endpoints for host management are not yet
# documented in the screenshots.  Paths below are best-effort placeholders.
# Verify and update from: https://io-api-lab.aexp.com/docs
# =====================================================================

class DisableObject(OneAPIAction):
    """Disable an ESXi host or Cluster in OneAPI/Grafana.

    Replaces:  DisableESXi  →  POST /icingaweb2/director/host?name={host}  (disabled=y)
    New call:  POST https://io-api-lab.aexp.com/hosts/disable
    TODO: Verify endpoint from https://io-api-lab.aexp.com/docs

    Required keys: host_name
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'disableObject'
        self.method = 'POST'
        self.path = '/hosts/disable'          # TODO: verify
        self.required_keys = ['host_name']
        self.body = {
            "object_name": data.get('host_name', ''),
            "disabled": True,
        }


class EnableObject(OneAPIAction):
    """Enable an ESXi host or Cluster in OneAPI/Grafana.

    Replaces:  EnableESXi  →  POST /icingaweb2/director/host?name={host}  (disabled=n)
    New call:  POST https://io-api-lab.aexp.com/hosts/enable
    TODO: Verify endpoint from https://io-api-lab.aexp.com/docs

    Required keys: host_name
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'enableObject'
        self.method = 'POST'
        self.path = '/hosts/enable'           # TODO: verify
        self.required_keys = ['host_name']
        self.body = {
            "object_name": data.get('host_name', ''),
            "disabled": False,
        }


class AddESXi(OneAPIAction):
    """Onboard a new ESXi host to OneAPI/Grafana.

    Replaces:  AddESXi  →  POST /icingaweb2/director/host
    New call:  POST https://io-api-lab.aexp.com/hosts
    TODO: Verify endpoint and body schema from https://io-api-lab.aexp.com/docs

    Required keys: host_name, location, hardwaretype, project, environment
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'addHost'
        self.method = 'POST'
        self.path = '/hosts'                   # TODO: verify
        self.required_keys = [
            'host_name', 'location', 'hardwaretype', 'project', 'environment'
        ]

        # Cisco hardware → always standalone
        if data.get('hardwaretype') == 'cisco':
            data['hardwaresubtype'] = 'standalone'

        self.body = {
            "object_name":          data.get('host_name', ''),
            "object_type":          "object",
            "address":              data.get('host_name', ''),
            "zone":                 data.get('zone', ''),
            "vars.env":             data.get('environment', ''),
            "vars.project":         data.get('project', ''),
            "vars.location":        data.get('location', ''),
            "vars.hardwaretype":    data.get('hardwaretype', ''),
            "vars.hardwaresubtype": data.get('hardwaresubtype', ''),
            "vars.shortname":       data.get('host_name', '').split('.')[0],
            "vars.sn_assignment_queue": data.get(
                'sn_assignment_queue', 'Infrastructure'
            ),
            "vars.sn_reporter_group": data.get(
                'sn_reporter_group', 'Infrastructure'
            ),
            "imports": "003-esx-automation-host-defaults",
        }

        if data.get('hydra') == 'true':
            self.body['vars.hydra'] = self.action
        if data.get('hardwaresubtype'):
            self.body['vars.hardwaresubtype'] = data['hardwaresubtype']
        if data.get('zone'):
            self.body['zone'] = data['zone']


class AddCluster(OneAPIAction):
    """Onboard a new Cluster to OneAPI/Grafana.

    Replaces:  AddCluster  →  POST /icingaweb2/director/host
    New call:  POST https://io-api-lab.aexp.com/hosts
    TODO: Verify endpoint and body schema from https://io-api-lab.aexp.com/docs

    Required keys: environment, host_name
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'addCluster'
        self.method = 'POST'
        self.path = '/hosts'                   # TODO: verify
        self.required_keys = ['environment', 'host_name']

        # Default zone mapping (PHX Instance zones)
        self.env_zone_mapping = {
            "dev":  "E1Checker",
            "qa":   "E2Checker",
            "gdha": "GDHAChecker",
        }

        split_string = data['host_name'].split('-')

        self.body = {
            "object_name":   data.get('host_name', ''),
            "object_type":   "object",
            "address":       data.get('host_name', ''),
            "zone":          self.env_zone_mapping.get(data.get('environment', ''), ''),
            "vars.env":      data.get('environment', ''),
            "imports":       "003-esx-automation-cluster-defaults",
            "vars.sn_assignment_queue": data.get(
                'sn_assignment_queue', 'Infrastructure'
            ),
            "vars.sn_reporter_group": data.get(
                'sn_reporter_group', 'Infrastructure'
            ),
            "vars.clustername": "-".join(split_string[1:]),
        }

        if data.get('hydra') == 'true':
            self.body['vars.hydra'] = self.action
        if data.get('zone'):
            self.body['zone'] = data['zone']


# =====================================================================
# Query Actions  (read-only lookups)
# =====================================================================

class GetHost(OneAPIAction):
    """Retrieve a host record from OneAPI/Grafana.

    Replaces:  GetHost  →  GET /icingaweb2/director/host?name={host}
    New call:  GET https://io-api-lab.aexp.com/hosts/<host_name>
    TODO: Verify endpoint from https://io-api-lab.aexp.com/docs

    Required keys: host_name
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'getHost'
        self.method = 'GET'
        host_name = data.get('host_name', '')
        self.path = f'/hosts/{host_name}'      # TODO: verify
        self.required_keys = ['host_name']
        self.body = {}


class GetAllHost(OneAPIAction):
    """Retrieve all hosts matching a pattern from OneAPI/Grafana.

    Replaces:  GetAllHost  →  GET /icingaweb2/director/hosts?q={host_name}
    New call:  GET https://io-api-lab.aexp.com/hosts?q=<host_name>
    TODO: Verify endpoint from https://io-api-lab.aexp.com/docs

    Required keys: host_name
    """

    def __init__(self, data: dict):
        super().__init__(data)
        self.action = 'getAllHost'
        self.method = 'GET'
        host_name = data.get('host_name', '')
        self.path = f'/hosts?q={host_name}'    # TODO: verify
        self.required_keys = ['host_name']
        self.body = {}
