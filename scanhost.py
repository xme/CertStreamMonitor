#!/usr/bin/env python3

# Copyright (c) 2018-2019 Caisse nationale d'Assurance Maladie
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import os
import re
import sys
import getopt
import logging
import datetime
import random
import socks
import signal
import json
from logging.handlers import RotatingFileHandler
from utils.confparser import ConfParser
from utils.utils import TimestampNow, VerifyPath
import sqlite3
import hues
import requests
import socket
from ipwhois import IPWhois
import warnings
import time
import safebrowsing
import apprise

def create_connection(db_file):
    """ create a database connection to the SQLite database
        specified by the db_file

    :param db_file: database file
    :return: Connection object or None
    """

    try:
        conn = sqlite3.connect(db_file, isolation_level=None)
        # debug SQL
        # conn.set_trace_callback(print)
        return conn
    except sqlite3.Error as e:
        print(e)
        return False


def args_parse():
    """
    Tools options
    """
    global ConfFile
    global fqdn_dirs
    fqdn_dirs = False

    if not len(sys.argv[1:]):
        usage()
    try:
        opts, args = getopt.getopt(sys.argv[1:], "hfc:", ["help", "fqdn-dirs", "conf="])
    except getopt.GetoptError as err:
        logging.error(" Option Error. Exiting..."+str(err))
        usage()
        sys.exit(2)

    for o, a in opts:
        if o in ("-h", "--help"):
            usage()
        elif o in ("-c", "--config"):
            if os.path.isfile(a):
                ConfFile = a
            else:
                logging.error(" Can't find configuration file. Exiting...")
                sys.exit(1)
        elif o in ("-f", "--fqdn-dirs"):
            fqdn_dirs = True
        else:
            assert False, "Unhandled Option"
    return


def usage():
    """
    CLI usage printing
    """
    usage = """
    -h --help       Print this help
    -c --config     Configuration file to use
    -f --fqdn-dirs  Store JSON files in sub-directories based on the hostname
     """
    print (usage)
    sys.exit(0)


def generate_alert_dir(path):
    """
    Generate the hashed directory path based on current date
    """
    # %m -> month
    # %d -> day
    # %Y -> year
    # %H -> hour
    # %M -> minute
    t_hour   = time.strftime("%H")
    t_minute = time.strftime("%M")
    t_day    = time.strftime("%d")
    t_month  = time.strftime("%m")
    t_year   = time.strftime("%Y")
    path = path.replace('%H', t_hour)
    path = path.replace('%M', t_minute)
    path = path.replace('%d', t_day)
    path = path.replace('%m', t_month)
    path = path.replace('%Y', t_year)
    return path


def ConfAnalysis(ConfFile):
    """
    configuration file analysis. Load global variables with parameters found
    in configuration file.

    :param  confFile: the configuration file
    """
    global CONF
    global DBFile
    global TABLEname
    global LogFile
    global Proxy
    global UA
    global UAFILE
    global Alerts_dir
    global MaxAttempts
    global Notification_Destination
    global Safe_Browsing_API_Key

    try:
        CONF = ConfParser(ConfFile)
        DBFile = CONF.DBFile
        TABLEname = CONF.TABLEname
        LogFile = CONF.LogFile
        Proxy = CONF.Proxy
        UA = CONF.http_UA
        Alerts_dir = generate_alert_dir(CONF.Alerts_dir)
        MaxAttempts = CONF.MaxAttempts
        Notification_Destination = CONF.Notification_Destination
        UAFILE = CONF.UAfile
        Safe_Browsing_API_Key = CONF.Safe_Browsing_API_Key

    except Exception as err:
        err = sys.exc_info()
        logging.error(" ConfParser Error: "+str(err))


def get_random_UserAgent_header(lines):
    """
    build a string containing a user-agent header, randomly
    choosen inside a given list

    :param lines: the file containing the user-agent possible values.
                  One value per line.
    :return: the header with user-agent value set.
    """
    ua = random.choice(lines)
    headers = {'user-agent': ua}
    return headers


def get_requests(hostname, lines, conn, Proxy):
    """
    build a requests object for a hostname

    :param hostname:
    :param lines: content of the file containing user-agents strings
    :param conn: connection to the database
    :param Proxy: connection through proxy

    :return: the answer to the request content or None
    """

    # if the certificate is a wildcard, display it but no testing.
    # and return.
    if '*' in hostname:
        hues.warn('wildcard certificate: no request for '+hostname)
        return None

    url = 'https://' + hostname
    headers = get_random_UserAgent_header(lines)

    # set proxy
    if Proxy:
        proxy = {"https": Proxy}
    else:
        proxy = ""

    try:
        r = requests.get(url, headers=headers, proxies=proxy, timeout=5)
        return r
    except requests.exceptions.SSLError as errs:
        # SSL error
        hues.error("  {} - SSL error".format(url))
        return None
    except requests.exceptions.ConnectionError as errc:
        # other connection error
        hues.error("  {} - Connection error".format(url))
        return None
    except requests.exceptions.RequestException as e:
        # A serious problem happened
        hues.error("  {} Error: {}".format(url, e))
        return None
    except KeyboardInterrupt:
        print("get_requests() - Interrupt received, stopping ...")
        print("start - committing, closing DB")
        conn.commit
        conn.close
        print("ending - committing, closing DB")
        sys.exit(0)
    except Exception as ex:
        hues.error("get_requests() - any other kind of error: {}".format(ex))
        return None


def get_webpage_title(request):
    """
    Get the website page title

    :param resquest: request object

    :return: webpage title or ""
    """
    try:
        page = request.text.strip()
        tit = re.search('<title>(.*?)</title>', page, re.IGNORECASE)
        if tit is not None:
            title = tit.group(1)
        else:
            title = ""
        return title
    except Exception as e:
        print("error in get_webpage_title(): "+str(e))
        return ""


def get_ASN_Infos(ipaddr):
    """
    Get Autonomous System Number informations linked to an ip address

    :param ipaddr: ip address of the website linked to the certificate common name

    :return: list of ASN infos: asn, asn_cidr, asn_country_code, asn_description, asn_abuse_email or the same with empty values
    """
    try:
        warnings.filterwarnings("ignore")
        obj = IPWhois(ipaddr)
        results = obj.lookup_rdap(depth=1)

        asn = results['asn']
        asn_cidr = results['asn_cidr']
        asn_country_code = results['asn_country_code']
        asn_description = results['asn_description']

        # parsing of all the entities members of the ASN record.
        # -> when finding an entity with 'abuse' role, print the email present
        #    in the contact object.
        try:
            for entity in results['objects'].values():
                if 'abuse' in entity['roles']:
                    asn_abuse_email = entity['contact']['email'][0]['value']
                    break
        except Exception as e:
            asn_abuse_email = ""

        return asn, asn_cidr, asn_country_code, asn_description, asn_abuse_email

    except Exception as e:
        asn, asn_cidr, asn_country_code, asn_description, asn_abuse_email = "", "", "", "", ""
        return asn, asn_cidr, asn_country_code, asn_description, asn_abuse_email


def scan_hostname(hostname, SerialNumber, lines, Proxy, conn, site_infos):
    """
    try scan a hostname and get informations back
    (HTTP code, page title, IP address, ASN, abuse email etc).

    :param hostname: the hostname present in the certificate
    :param SerialNumber: the serial number of the certificate
    :param lines: list of user-agents strings
    :param Proxy: proxy settings
    :param conn: database connection
    :param site_infos: informations extracted on the net for the given hostname

    :return: True if everything went fine, False if any problem has been encountered
    """

    title = ""
    try:
        r = get_requests(hostname, lines, conn, Proxy)
        if r is not None:
            hues.success('HTTP '+str(r.status_code)+' - ' + hostname)

            # retrieve the title of the homepage
            title = get_webpage_title(r)

            # retrieve ASN informations
            ipaddr = socket.gethostbyname(hostname)
            asn, asn_cidr, asn_country_code, asn_description, asn_abuse_email = get_ASN_Infos(
                ipaddr)

            # retrieve Google Safe Browsing Lookup API status for this hostname
            if Safe_Browsing_API_Key is not '':
                sb = safebrowsing.LookupAPI(Safe_Browsing_API_Key) 
                safe_browsing_status = sb.threat_matches_find(hostname)
            else:
                safe_browsing_status = "No API key in config file"

            # build the content of the alert file using certificate / webpage / ASN informations
            site_infos = {
                'hostname': hostname,
                'http_code': r.status_code,
                'cert_serial_number': SerialNumber,
                'webpage_title': title,
                'ip_addr': ipaddr,
                'asn': asn,
                'asn_cidr': asn_cidr,
                'asn_country_code': asn_country_code,
                'asn_description': asn_description,
                'asn_abuse_email': asn_abuse_email,
                'safe_browsing_status': safe_browsing_status
            }
            return site_infos
        else:
            return {}

    except KeyboardInterrupt:
        print("scan_hostname() - Interrupt received, stopping ...")
        print("start - committing, closing DB")
        conn.commit
        conn.close
        print("ending - committing, closing DB")
        sys.exit(0)

    except Exception as ex:
        hues.error("scan_hostname() - any other kind of error: {}".format(ex))
        return {}


def parse_and_scan_all_hostnames(TABLEname, Proxy, conn):
    """
    Parse and scan all hostnames present in DB and having StillInvestig set to null or ""

    :param TABLEname: the table name storing certificate informations in database
    :param Proxy: proxy value
    :param conn: db connection

    :return: True if everything went fine, False if smething went wrong    
    """
    try:
        # Query rows that have not StillInvestig column already set
        # get Domain and Fingerprint column
        cur = conn.cursor()
        cur.execute("SELECT Domain,Fingerprint,StillInvestig FROM "+TABLEname +
                    " WHERE StillInvestig IS NULL or StillInvestig = '' or StillInvestig LIKE '_'")
        rows = cur.fetchall()

        # creating Alerts_dir if don't exist
        try:
            os.makedirs(Alerts_dir, mode=0o777, exist_ok=True)
        except FileExistsError:
            pass
        except:
            err = sys.exc_info()
            logging.error(" Can't create Alerts_dir: "+str(err))

        # read User Agent file
        try:
            lines = open(UAFILE).read().splitlines()
        except:
            lines = UA

        # load apprise instance with config file parameters if notifications are activated in the config file
        if Notification_Destination is not '':
            apobj = apprise.Apprise()
            apobj.add(Notification_Destination)

        # run scan on each hostname
        for row in rows:
            hostname = row[0]
            SerialNumber = row[1]
            Attempts = row[2]
            site_infos = {}

            if Attempts is None:
                Attempts = 0
            else:
                Attempts = int(Attempts)

            site_infos = scan_hostname(hostname, SerialNumber, lines, Proxy, conn, site_infos)

            if not site_infos:
                # Save the MaxAttempts counter incremented by 1
                Attempts += 1
                if Attempts > MaxAttempts:
                    Attempts = "Disabled"
                    hues.warn('Max attempts reached for '+hostname)
                cur.execute("UPDATE "+TABLEname+" SET StillInvestig= ? WHERE Domain = ? AND Fingerprint = ? ;",
                            (str(Attempts), hostname, SerialNumber))
                continue
            else:
                # if the site is UP, we log the timestamp in the database in order to not reprocess it
                cur.execute("UPDATE "+TABLEname+" SET StillInvestig= ? WHERE Domain = ? AND Fingerprint = ? ;",
                            (format(datetime.datetime.utcnow().replace(microsecond=0).isoformat()), hostname, SerialNumber))
                conn.commit
                if fqdn_dirs:
                    # Split hostname into a reverse list (TLD first)
                    words = hostname.split(".")[::-1]
                    FQDN_dir = Alerts_dir
                    for w in words:
                        FQDN_dir = FQDN_dir + "/" + w
                    try:
                        os.makedirs(FQDN_dir, mode=0o777, exist_ok=True)
                    except FileExistsError:
                        pass
                    except:
                        err = sys.exc_info()
                        logging.error(" Can't create Alerts_dir: "+str(err))
                    print("Creating "+FQDN_dir+"/"+hostname+".json : " + str(site_infos))
                    f = open(FQDN_dir+"/"+hostname+".json", "w")
                else:
                    print("Creating "+Alerts_dir+"/"+hostname+".json : " + str(site_infos))
                    # log the hostname under the form of a file under the /alerts subdirectory
                    # + fill the file with informations like ASN/abuse email/IP/web page title etc
                    # next task: the SOC/Cert has to investigate this host.
                    f = open(Alerts_dir+"/"+hostname+".json", "w")
                json.dump(site_infos, f, indent=4)
                f.close()
                if Notification_Destination is not '':
                    body_site_infos = str(site_infos).replace(', \'',chr(10)+'\'')
                    body_site_infos = body_site_infos.replace('{','')
                    body_site_infos = body_site_infos.replace('}','')
                    apobj.notify(title="[CertStreamMonitor] Alert: "+hostname, body=body_site_infos,)

        return True

    except KeyboardInterrupt:
        print("Interrupt received, stopping ...")
        print("start - committing, closing DB")
        conn.commit
        conn.close
        print("ending - committing, closing DB")
        return False

    except Exception as e:
        hues.error("parse_and_scan_all_hostnames function error: {}".format(e))
        return False

    finally:
        conn.commit
        conn.close


def main():
    ConfAnalysis(ConfFile)

    # create a database connection
    conn = create_connection(DBFile)

    with conn:
        print("Test all domains in DB for Internet Presence:")
        print("*********************************************")
        parse_and_scan_all_hostnames(TABLEname, Proxy, conn)


if __name__ == '__main__':
    args_parse()
    main()
