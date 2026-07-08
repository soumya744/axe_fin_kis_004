import argparse
import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(Path(__file__).parents[0]))

import logging
logger = logging.getLogger(__name__)

from api_oneapi_models import (
    GetHost, GetAllHost,
    AddESXi, AddCluster,
    DisableObject, EnableObject,
    ScheduleSilence, RemoveSilence, GetSilenceStatus,
)

# == [ MAIN ] =====================================================
ACTION_KEYMAP = {
    'getHost':            GetHost,
    'getAllHost':          GetAllHost,
    'addHost':            AddESXi,
    'addCluster':         AddCluster,
    'disableHost':        DisableObject,
    'enableHost':         EnableObject,
    'scheduleSilence':    ScheduleSilence,
    'removeSilence':      RemoveSilence,
    'getSilenceStatus':   GetSilenceStatus,

    # Legacy Icinga action names → mapped to new OneAPI classes
    'addESXi':            AddESXi,
    'disableESXi':        DisableObject,
    'enableESXi':         EnableObject,
    'scheduleDowntime':   ScheduleSilence,
    'removeDowntime':     RemoveSilence,
}


def main(data):
    print("Starting api_oneapi_consumer....")
    try:
        action_cls = ACTION_KEYMAP.get(data.get('action'))
        if action_cls is None:
            error = 'No valid action was provided: ' + str(data.get('action'))
            print("KeyError")
            print(error)
            data['icinga_consumer_error'] = error
            return data

        action_obj = action_cls(data)
        output = action_obj.output()
    except KeyError as e:
        error = 'No valid action was provided: ' + str(e)
        print("KeyError")
        print(error)
        data['icinga_consumer_error'] = error
        return data
    except Exception as e:
        from api_oneapi_models import ConsumerException
        if isinstance(e, ConsumerException):
            logger.error(e.message)
            data['icinga_consumer_error'] = e.message
        else:
            data['icinga_consumer_error'] = str(e)
        return data

    return output


# == [ COMMAND LINE ] =============================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='OneAPI command line consumer  (migrated from Icinga Director)'
    )

    # Connection / auth
    parser.add_argument('--api_url',    default='',  help='OneAPI base URL override')
    parser.add_argument('--token',      default='',  help='Bearer token (skips AuthBlue)')
    parser.add_argument('-req', '--requestor', default='', action='store',
                        help='User name (ADS) of requestor')

    # Object identification
    parser.add_argument('-o', '--object_type', required=False, action='store',
                        help='Object type (host / cluster)')
    parser.add_argument('-l', '--host_name',   required=False, action='store',
                        help='Host name')
    parser.add_argument('-a', '--address',     required=False, action='store',
                        help='Host address')
    parser.add_argument('-z', '--zone',        action='store',  help='Host zone')
    parser.add_argument('-e', '--environment', required=False, action='store',
                        help='Host environment')
    parser.add_argument('-pr', '--project',    required=False, action='store',
                        help='Host project')
    parser.add_argument('-loc', '--location',  required=False, action='store',
                        help='Host location')
    parser.add_argument('-t', '--hardwaretype',  required=False, action='store',
                        help='Host hardware type')
    parser.add_argument('-s', '--hardwaresubtype', required=False, action='store',
                        help='Host hardware sub type')
    parser.add_argument('-c', '--imports',     default='003-esx-automation-host-defaults',
                        help='Import template')
    parser.add_argument('--hydra',             default='', help='Is Hydra. true|false')
    parser.add_argument('-sn', '--service',    default='', help='Service name')
    parser.add_argument('-bc', '--bulkClusters', default='',
                        help='Clusters to be added in bulk')
    parser.add_argument('-bh', '--bulkHosts',    default='',
                        help='Hosts to be added in bulk')

    # Action & scheduling
    parser.add_argument('-x', '--action',  required=True, action='store',
                        help='Action to perform')
    parser.add_argument('--start', required=False, action='store',
                        help='Start datetime  (dd/MM/YYYY HH:MM  or  ISO)')
    parser.add_argument('--end',   required=False, action='store',
                        help='End datetime    (dd/MM/YYYY HH:MM  or  ISO)')
    parser.add_argument('--comment', required=False, action='store',
                        help='Comment / reason')

    # Silence-specific
    parser.add_argument('--job_id',  required=False, action='store',
                        help='Silence job_id returned by scheduleSilence')

    # Notifications
    parser.add_argument('-se', '--send_email', default='false', required=False,
                        action='store', help='Send notification email (true|false)')

    args = parser.parse_args()
    data = vars(args)

    # If --token passed, inject it as env var so the model picks it up
    if data.get('token'):
        os.environ['ONEAPI_BEARER_TOKEN'] = data['token']

    main(data)
