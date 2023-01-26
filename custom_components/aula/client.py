"""
Aula client
Based on https://github.com/JBoye/HA-Aula
"""
import logging
import requests
import datetime
import requests
from bs4 import BeautifulSoup
import json, re
from .const import API, API_VERSION, MIN_UDDANNELSE_API, MEEBOOK_API, SYSTEMATIC_API
from homeassistant.exceptions import ConfigEntryNotReady

_LOGGER = logging.getLogger(__name__)
class Client:
    huskeliste = {}
    presence = {}
    ugep_attr = {}
    ugepnext_attr = {}
    widgets = {}
    tokens = {}
    muopgaver = {}
    muopgaver_attr = {}

    def __init__(self, username, password, schoolschedule, ugeplan):
        self._username = username
        self._password = password
        self._session = None
        self._schoolschedule = schoolschedule
        self._ugeplan = ugeplan

    def login(self):
        _LOGGER.debug("Logging in")
        self._session = requests.Session()
        response = self._session.get('https://login.aula.dk/auth/login.php?type=unilogin', verify=True)

        user_data = { 'username': self._username, 'password': self._password, 'selected-aktoer': "KONTAKT" }
        redirects = 0
        success = False
        url = ''
        while success == False and redirects < 10:
            html = BeautifulSoup(response.text, 'lxml')
            url = html.form['action']
            post_data = {}
            for input in html.find_all('input'):
                if(input.has_attr('name') and input.has_attr('value')):
                    post_data[input['name']] = input['value']
                    for key in user_data:
                        if(input.has_attr('name') and input['name'] == key):
                            post_data[key] = user_data[key]

            response = self._session.post(url, data = post_data, verify=True)
            if response.url == 'https://www.aula.dk:443/portal/':
                success = True
            redirects += 1

        # Find the API url in case of a version change
        self.apiurl = API+API_VERSION
        apiver = int(API_VERSION)
        api_success = False
        while api_success == False:
            _LOGGER.debug("Trying API at "+self.apiurl)
            ver = self._session.get(self.apiurl + "?method=profiles.getProfilesByLogin", verify=True)
            if ver.status_code == 410:
                _LOGGER.warning("API was expected at "+self.apiurl+" but responded with HTTP 410. The integration will automatically try a newer version and everything may work fine.")
                apiver += 1
            if ver.status_code == 403:
                msg = "Access to Aula API was denied. Please check that you have entered the correct credentials."
                _LOGGER.error(msg)
                raise ConfigEntryNotReady(msg)
            elif ver.status_code == 200:
                self._profiles = ver.json()["data"]["profiles"]
                #_LOGGER.debug("self._profiles "+str(self._profiles))
                api_success = True
            self.apiurl = API+str(apiver)
        _LOGGER.debug("Found API on "+self.apiurl)
        #

        #ver = self._session.get(self.apiurl + "?method=profiles.getProfilesByLogin", verify=True)
        #self._profiles = ver.json()["data"]["profiles"]
        self._profilecontext = self._session.get(self.apiurl + "?method=profiles.getProfileContext&portalrole=guardian", verify=True).json()['data']['institutionProfile']['relations']
        _LOGGER.debug("LOGIN: " + str(success))
        _LOGGER.debug("Config - schoolschedule: "+str(self._schoolschedule)+", config - ugeplaner: "+str(self._ugeplan))

    def get_widgets(self):
        detected_widgets = self._session.get(self.apiurl + "?method=profiles.getProfileContext", verify=True).json()["data"]["moduleWidgetConfiguration"]["widgetConfigurations"]
        for widget in detected_widgets:
            widgetid = str(widget["widget"]["widgetId"])
            widgetname = widget["widget"]["name"]
            self.widgets[widgetid] = widgetname
        _LOGGER.debug("Widgets found: "+str(self.widgets))

    def get_token(self,widgetid,mock=False):
        _LOGGER.debug("Requesting token for widget "+widgetid)
        if mock:
            return "MockToken"
        self._bearertoken = self._session.get(self.apiurl + "?method=aulaToken.getAulaToken&widgetId="+widgetid, verify=True).json()["data"]
        token = "Bearer "+str(self._bearertoken)
        self.tokens[widgetid] = token
        return token
        
    def update_data(self):
        is_logged_in = False
        if self._session:
            response = self._session.get(self.apiurl + "?method=profiles.getProfilesByLogin", verify=True).json()
            is_logged_in = response["status"]["message"] == "OK"

        _LOGGER.debug("is_logged_in? " + str(is_logged_in))

        if not is_logged_in:
            self.login()

        self._childnames = {}
        self._institutions = {}
        self._childuserids = []
        self._childids = []
        self._children = []
        self._institutionProfiles = []
        for profile in self._profiles:
            for child in profile["children"]:
                self._childnames[child["id"]] = child["name"]
                self._institutions[child["id"]] = child["institutionProfile"]["institutionName"]
                self._children.append(child)
                self._childids.append(str(child["id"]))
                self._childuserids.append(str(child["userId"]))
            for institutioncode in profile["institutionProfiles"]:
                if str(institutioncode["institutionCode"]) not in self._institutionProfiles:
                    self._institutionProfiles.append(str(institutioncode["institutionCode"]))
        _LOGGER.debug("Child ids and names: "+str(self._childnames))
        _LOGGER.debug("Child ids and institution names: "+str(self._institutions))
        _LOGGER.debug("Institution codes: "+str(self._institutionProfiles))
        
        self._daily_overview = {}
        for i, child in enumerate(self._children):
            response = self._session.get(self.apiurl + "?method=presence.getDailyOverview&childIds[]=" + str(child["id"]), verify=True).json()
            if len(response["data"]) > 0:
                self.presence[str(child["id"])] = 1
                self._daily_overview[str(child["id"])] = response["data"][0]
            else:
                _LOGGER.warn("Unable to retrieve presence data from Aula from child with id "+str(child["id"])+". Some data will be missing from sensor entities.")
                self.presence[str(child["id"])] = 0
        _LOGGER.debug("Child ids and presence data status: "+str(self.presence))

        # Messages:
        mesres = self._session.get(self.apiurl + "?method=messaging.getThreads&sortOn=date&orderDirection=desc&page=0", verify=True)
        self.unread_messages = 0
        self.message = {}
        for mes in mesres.json()["data"]["threads"]:
            if not mes["read"]:
                self.unread_messages = 1
                threadid = mes["id"]
                break
        if self.unread_messages == 1:
            _LOGGER.debug("tid "+str(threadid))
            threadres = self._session.get(self.apiurl + "?method=messaging.getMessagesForThread&threadId="+str(threadid)+"&page=0", verify=True)
            for message in threadres.json()["data"]["messages"]:
                if message["messageType"] == "Message":
                    try:
                        self.message["text"] = message["text"]["html"]
                    except:
                        try:
                            self.message["text"] = message["text"]
                        except:
                            self.message["text"] = "intet indhold..."
                            _LOGGER.warning("There is an unread message, but we cannot get the text.")
                    try:
                        self.message["sender"] = message["sender"]["fullName"]
                    except:
                        self.message["sender"] = "Ukendt afsender"
                    break
                try:
                    self.message["subject"] = threadres.json()["data"]["subject"]
                except:
                    self.message["subject"] = ""
                break
            #_LOGGER.debug("Full dump of Aula messages: "+str(threadres.text))

        # Calendar:
        if self._schoolschedule == True:
            instProfileIds = ",".join(self._childids)
            csrf_token = self._session.cookies.get_dict()["Csrfp-Token"]
            headers = {'csrfp-token': csrf_token, 'content-type': 'application/json'}
            start = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d 00:00:00.0000%z')
            _end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=14)
            end = _end.strftime('%Y-%m-%d 00:00:00.0000%z')
            post_data = '{"instProfileIds":['+instProfileIds+'],"resourceIds":[],"start":"'+start+'","end":"'+end+'"}'
            _LOGGER.debug("Fetching calendars...")
            #_LOGGER.debug("Calendar post-data: "+str(post_data))
            res = self._session.post(self.apiurl + "?method=calendar.getEventsByProfileIdsAndResourceIds",data=post_data,headers=headers, verify=True)
            try:
                with open('skoleskema.json', 'w') as skoleskema_json:
                    json.dump(res.text, skoleskema_json)
            except:
               _LOGGER.warn("Got the following reply when trying to fetch calendars: "+str(res.text))
        # End of calendar

        # MU Opgaver:
        mock_muopgaver = 0
        if len(self.widgets) == 0:
            self.get_widgets()
        if "0030" in self.widgets or mock_muopgaver == 1:
            _LOGGER.debug("Proceeding with MU Opgaver...")
            now = datetime.datetime.now() + datetime.timedelta(weeks=1)
            thisweek = datetime.datetime.now().strftime('%Y-W%W')
            childUserIds = ",".join(self._childuserids)
            # Fix multiple gets:
            guardian = self._session.get(self.apiurl + "?method=profiles.getProfileContext&portalrole=guardian", verify=True).json()["data"]["userId"]
            #
            muopgaverpayload = '/opgaveliste?placement=narrow&sessionUUID='+guardian+'&userProfile=guardian&currentWeekNumber='+thisweek+'&childFilter='+childUserIds
            if mock_muopgaver == 1:
                muopgaver = '{"opgaver":[{"id":"75c55f039b5943069dcb9afe260dc5fd","opgaveType":"SimpelLektie","title":"HUSK BIBLIOTEKSBØGER","afleveringsdato":"\/Date(1674601200000-0000)\/","ugedag":"Onsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZDc1YzU1ZjAzLTliNTktNDMwNi05ZGNiLTlhZmUyNjBkYzVmZCUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502989,"navn":"Dansk 3C","fagId":1331,"fagNavn":"Dansk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"},{"id":"0bb0c135e00040d0a691aff57afd69f5","opgaveType":"SimpelLektie","title":"Kreativ værksted","afleveringsdato":"\/Date(1674428400000-0000)\/","ugedag":"Mandag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjg5MDM3NSUzZm9wZ2F2ZSUzZDBiYjBjMTM1LWUwMDAtNDBkMC1hNjkxLWFmZjU3YWZkNjlmNSUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1642440,"navn":"SFO 0. årgang","fagId":4342,"fagNavn":"SFO"}],"unilogin":"noah217a","kuvertnavn":"Fornavn Efternavnsen","placering":"Nederst"},{"id":"85f7ba201e6f47489153df5b4533248d","opgaveType":"SimpelLektie","title":"kreativ værksted","afleveringsdato":"\/Date(1674514800000-0000)\/","ugedag":"Tirsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjg5MDM3NSUzZm9wZ2F2ZSUzZDg1ZjdiYTIwLTFlNmYtNDc0OC05MTUzLWRmNWI0NTMzMjQ4ZCUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1642440,"navn":"SFO 0. årgang","fagId":4342,"fagNavn":"SFO"}],"unilogin":"noah217a","kuvertnavn":"Fornavn Efternavnsen","placering":"Nederst"},{"id":"aea080c2954446a19fe82221bc5f38c4","opgaveType":"SimpelLektie","title":"kreativ værksted","afleveringsdato":"\/Date(1674601200000-0000)\/","ugedag":"Onsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjg5MDM3NSUzZm9wZ2F2ZSUzZGFlYTA4MGMyLTk1NDQtNDZhMS05ZmU4LTIyMjFiYzVmMzhjNCUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1642440,"navn":"SFO 0. årgang","fagId":4342,"fagNavn":"SFO"}],"unilogin":"noah217a","kuvertnavn":"Fornavn Efternavnsen","placering":"Nederst"},{"id":"0ca8b8bf6a574df6a9a3b88fc573a036","opgaveType":"SimpelLektie","title":"Vi bager","afleveringsdato":"\/Date(1674687600000-0000)\/","ugedag":"Torsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjg5MDM3NSUzZm9wZ2F2ZSUzZDBjYThiOGJmLTZhNTctNGRmNi1hOWEzLWI4OGZjNTczYTAzNiUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1642440,"navn":"SFO 0. årgang","fagId":4342,"fagNavn":"SFO"}],"unilogin":"noah217a","kuvertnavn":"Fornavn Efternavnsen","placering":"Nederst"},{"id":"18e81c198e46425abefe68c3e6cb9fba","opgaveType":"SimpelLektie","title":"Vi hygger på stuen","afleveringsdato":"\/Date(1674774000000-0000)\/","ugedag":"Fredag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjg5MDM3NSUzZm9wZ2F2ZSUzZDE4ZTgxYzE5LThlNDYtNDI1YS1iZWZlLTY4YzNlNmNiOWZiYSUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1642440,"navn":"SFO 0. årgang","fagId":4342,"fagNavn":"SFO"}],"unilogin":"noah217a","kuvertnavn":"Fornavn Efternavnsen","placering":"Nederst"},{"id":"09437f531b7f4d3cb9591e1c5439f8c7","opgaveType":"SimpelLektie","title":"LEKTIE:Engelsk: Lav side 16 og 18 i den grå mappe - i afsnittet Animals. Jeg samler mapperne ind, så er der huller må de også laves. :-)","afleveringsdato":"\/Date(1674428400000-0000)\/","ugedag":"Mandag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZDA5NDM3ZjUzLTFiN2YtNGQzYy1iOTU5LTFlMWM1NDM5ZjhjNyUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502991,"navn":"Engelsk 3C","fagId":1341,"fagNavn":"Engelsk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"},{"id":"e08494a12d1e47688e9380df500242bc","opgaveType":"SimpelLektie","title":"Lektie:Dansk: Øv kapitel 8 - find 3 navneord, 3 udsagnsord og 3 tillægsord og skriv dem på det udleverede ark.","afleveringsdato":"\/Date(1674514800000-0000)\/","ugedag":"Tirsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZGUwODQ5NGExLTJkMWUtNDc2OC04ZTkzLTgwZGY1MDAyNDJiYyUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502989,"navn":"Dansk 3C","fagId":1331,"fagNavn":"Dansk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"},{"id":"179d640ee2ce4454909643f23581333a","opgaveType":"SimpelLektie","title":"LEKTIE: Engelsk: Øv side 35.","afleveringsdato":"\/Date(1674514800000-0000)\/","ugedag":"Tirsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZDE3OWQ2NDBlLWUyY2UtNDQ1NC05MDk2LTQzZjIzNTgxMzMzYSUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502989,"navn":"Dansk 3C","fagId":1331,"fagNavn":"Dansk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"},{"id":"6664e703516c46c6a83a51c4ba622d61","opgaveType":"SimpelLektie","title":"MÅ: Lav KREA til Pelle","afleveringsdato":"\/Date(1674514800000-0000)\/","ugedag":"Tirsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZDY2NjRlNzAzLTUxNmMtNDZjNi1hODNhLTUxYzRiYTYyMmQ2MSUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502989,"navn":"Dansk 3C","fagId":1331,"fagNavn":"Dansk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"},{"id":"41ef3a40791946879c90c71316393173","opgaveType":"SimpelLektie","title":"LEKTIE: Find 5 navneord, 5 udsagnsord og 5 tillægsord i Pelle-teksten - så der ialt er 8 ord af hver ordklasse. ","afleveringsdato":"\/Date(1674601200000-0000)\/","ugedag":"Onsdag","ugenummer":4,"erFaerdig":false,"url":"https://api.minuddannelse.net/aula/redirect/731004/aHR0cHMlM2ElMmYlMmZ3d3cubWludWRkYW5uZWxzZS5uZXQlMmZOb2RlJTJmbWludWdlJTJmMjQ4NjM4NCUzZm9wZ2F2ZSUzZDQxZWYzYTQwLTc5MTktNDY4Ny05YzkwLWM3MTMxNjM5MzE3MyUyNnVnZSUzZDIwMjMtVzQ=","hold":[{"id":1502989,"navn":"Dansk 3C","fagId":1331,"fagNavn":"Dansk"}],"unilogin":"neya0020","kuvertnavn":"Emilie Efternavnsen","placering":"Nederst"}]}'
                self.muopgaver_json = json.loads(muopgaver, strict=False)
            else:
                muopgavertoken = self.get_token("0030")
                muopgaver = requests.get(MIN_UDDANNELSE_API + muopgaverpayload, headers={"Authorization": muopgavertoken, "accept":"application/json"}, verify=True)
                _LOGGER.debug("Got the following response from MU opgaver: "+str(muopgaver.text))
                self.muopgaver_json = muopgaver.json()

            days = ["Mandag","Tirsdag","Onsdag","Torsdag","Fredag"]
            for fullname in self._childnames:
                fornavn = self._childnames[fullname].split()[0]
                #_LOGGER.debug("NAME "+fornavn)
                dayson = {}
                for day in days:
                    #dayson.setdefault(day, "")
                    for opgave in self.muopgaver_json["opgaver"]:
                        if opgave["kuvertnavn"].split()[0] == fornavn and opgave["ugedag"] == day:
                            #_LOGGER.debug("Found MU opgaver for "+fornavn+" :"+str(opgave))
                            self.muopgaver_attr.setdefault(fornavn, "")
                            #self.muopgaver_attr[fornavn] = self.muopgaver_attr[fornavn]+opgave["title"]
                            dayson.setdefault(day, "")
                            dayson[day] = dayson[day]+"<b>"+opgave["hold"][0]["fagNavn"]+"</b>"+opgave["title"]
                            #self.muopgaver_attr[fornavn] = self.muopgaver_attr[fornavn]+dayson
                if fornavn in self.muopgaver_attr:
                        #_LOGGER.debug(fornavn+" found!")
                    self.muopgaver_attr[fornavn] = dayson
                #_LOGGER.debug("dayson "+str(dayson))
                    #for d in days:
                    #    if opgave["ugedag"] == d:
                    #        self.muopgaver_attr.setdefault(fornavn, d)
                            #_LOGGER.debug("hhhhhhhhhhhhhhhhhhhhhhhhhhhh "+str(opgave["title"]))
                    #day = opgave["ugedag"]
                    #self.muopgaver_attr[day]
                    #self.muopgaver_attr[fornavn]
                    #_LOGGER.debug(str(opgave))
                    #if opgave["kuvertnavn"].split()[0] == fornavn:
                    #    self.muopgaver[fornavn] = 1
                    #    break
                    #    try:
                    #        self.muopgaver_attr[fornavn][opgave["ugedag"]] += opgave["title"]
                    #    except:
                    #        self.muopgaver_attr[fornavn][opgave["ugedag"]] = opgave["title"]
                        
            _LOGGER.debug("self.muopgaver_attr: "+str(self.muopgaver_attr))
            #_LOGGER.debug("self.muopgaver: "+str(self.muopgaver))
            # self.ugep_attr[person["navn"].split()[0]] = ugeplan

        # Ugeplaner:
        if self._ugeplan == True:
            guardian = self._session.get(self.apiurl + "?method=profiles.getProfileContext&portalrole=guardian", verify=True).json()["data"]["userId"]
            childUserIds = ",".join(self._childuserids)
            
            if len(self.widgets) == 0:
                self.get_widgets()
            if not "0029" in self.widgets and not "0004" in self.widgets and not "0062" in self.widgets:
                _LOGGER.error("You have enabled ugeplaner, but we cannot find any matching widgets (0029,0004) in Aula.")
            if "0029" in self.widgets and "0004" in self.widgets:
                _LOGGER.warning("Multiple sources for ugeplaner is untested and might cause problems.")

            def ugeplan(week,thisnext):
                if "0029" in self.widgets:
                    token = self.get_token("0029")
                    get_payload = '/ugebrev?assuranceLevel=2&childFilter='+childUserIds+'&currentWeekNumber='+week+'&isMobileApp=false&placement=narrow&sessionUUID='+guardian+'&userProfile=guardian'
                    ugeplaner = requests.get(MIN_UDDANNELSE_API + get_payload, headers={"Authorization":token, "accept":"application/json"}, verify=True)
                    #_LOGGER.debug("ugeplaner status_code "+str(ugeplaner.status_code))
                    #_LOGGER.debug("ugeplaner response "+str(ugeplaner.text))
                    for person in ugeplaner.json()["personer"]:
                        ugeplan = person["institutioner"][0]["ugebreve"][0]["indhold"]
                        if thisnext == "this":
                            self.ugep_attr[person["navn"].split()[0]] = ugeplan
                        elif thisnext == "next":
                            self.ugepnext_attr[person["navn"].split()[0]] = ugeplan

                if "0062" in self.widgets:
                    _LOGGER.debug("In the Huskelisten flow...")
                    token = self.get_token("0062", False)
                    huskelisten_headers = {
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
                        "Aula-Authorization": token,
                        "Origin": "https://www.aula.dk",
                        "Referer": "https://www.aula.dk/",
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "cross-site",
                        "User-Agent": "Mozilla/5.0 (X11; CrOS x86_64 15183.51.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
                        "zone": "Europe/Copenhagen"
                    }

                    children = "&children=".join(self._childuserids)
                    institutions = "&institutions=".join(self._institutionProfiles)
                    timedelta = datetime.datetime.now() + datetime.timedelta(days=180)
                    From = datetime.datetime.now().strftime('%Y-%m-%d')
                    dueNoLaterThan = timedelta.strftime("%Y-%m-%d")
                    get_payload = '/reminders/v1?children='+children+'&from='+From+'&dueNoLaterThan='+dueNoLaterThan+'&widgetVersion=1.10&userProfile=guardian&sessionId='+self._username+'&institutions='+institutions
                    _LOGGER.debug("Huskelisten get_payload: "+SYSTEMATIC_API+get_payload)
                    #
                    mock_huskelisten = 0
                    #
                    if mock_huskelisten == 1:
                        _LOGGER.warning("Using mock data for Huskelisten.")
                        mock_huskelisten = '[{"userName":"Emilie efternavn","userId":164625,"courseReminders":[],"assignmentReminders":[],"teamReminders":[{"id":76169,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-11-29T23:00:00Z","teamId":65240,"teamName":"2A","reminderText":"Onsdagslektie: Matematikfessor.dk: Sænk skibet med plus.","createdBy":"Peter ","lastEditBy":"Peter ","subjectName":"Matematik"},{"id":76598,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-12-06T23:00:00Z","teamId":65240,"teamName":"2A","reminderText":"Julekalender på Skoledu.dk: I skal forsøge at løse dagens kalenderopgave. opgaven kan også godt løses dagen efter.","createdBy":"Peter ","lastEditBy":"Peter Riis","subjectName":"Matematik"},{"id":76599,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-12-13T23:00:00Z","teamId":65240,"teamName":"2A","reminderText":"Julekalender på Skoledu.dk: I skal forsøge at løse dagens kalenderopgave. opgaven kan også godt løses dagen efter.","createdBy":"Peter ","lastEditBy":"Peter ","subjectName":"Matematik"},{"id":76600,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-12-20T23:00:00Z","teamId":65240,"teamName":"2A","reminderText":"Julekalender på Skoledu.dk: I skal forsøge at løse dagens kalenderopgave. opgaven kan også godt løses dagen efter.","createdBy":"Peter Riis","lastEditBy":"Peter Riis","subjectName":"Matematik"}]},{"userName":"Karla","userId":77882,"courseReminders":[],"assignmentReminders":[{"id":0,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-12-08T11:00:00Z","courseId":297469,"teamNames":["5A","5B"],"teamIds":[65271,65258],"courseSubjects":[],"assignmentId":5027904,"assignmentText":"Skriv en novelle"}],"teamReminders":[{"id":76367,"institutionName":"Holme Skole","institutionId":183,"dueDate":"2022-11-30T23:00:00Z","teamId":65258,"teamName":"5A","reminderText":"Læse resten af kap.1 fra Ternet Ninja ( kopiark) Læs det hele højt eller vælg et afsnit. ","createdBy":"Christina ","lastEditBy":"Christina ","subjectName":"Dansk"}]},{"userName":"Vega  ","userId":206597,"courseReminders":[],"assignmentReminders":[],"teamReminders":[]}]'
                        data = json.loads(mock_huskelisten, strict=False)
                    else:
                        response = requests.get(SYSTEMATIC_API + get_payload, headers=huskelisten_headers, verify=True)
                        try:
                            data = json.loads(response.text, strict=False)
                        except:
                            _LOGGER.error("Could not parse the response from Huskelisten as json.")
                        #_LOGGER.debug("Huskelisten raw response: "+str(response.text))

                    for person in data:
                        name = person["userName"].split()[0]
                        _LOGGER.debug("Huskelisten for "+name)
                        huskel = ''
                        reminders = person["teamReminders"]
                        if len(reminders) > 0:
                            for reminder in reminders:
                                mytime = datetime.datetime.strptime(reminder["dueDate"],"%Y-%m-%dT%H:%M:%SZ")
                                ftime = mytime.strftime('%A %d. %B')
                                huskel = huskel+"<h3>"+ftime+"</h3>"
                                huskel = huskel+"<b>"+reminder["subjectName"]+"</b><br>"
                                huskel = huskel+"af "+reminder["createdBy"]+"<br><br>"
                                content = re.sub(r"([0-9]+)(\.)", r"\1\.", reminder["reminderText"])
                                huskel = huskel+content+"<br><br>"
                        else:
                            huskel = huskel+str(name)+" har ingen påmindelser."
                        self.huskeliste[name] = huskel
                       
                # End Huskelisten
                if "0004" in self.widgets:
                    # Try Meebook:
                    _LOGGER.debug("In the Meebook flow...")
                    token = self.get_token("0004")
                    #_LOGGER.debug("Token "+token)
                    headers = {
                        "authority": "app.meebook.com",
                        "accept": "application/json",
                        "authorization": token,
                        "dnt": "1",
                        "origin": "https://www.aula.dk",
                        "referer": "https://www.aula.dk/",
                        "sessionuuid": self._username,
                        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36",
                        "x-version": "1.0"
                    }
                    childFilter = "&childFilter[]=".join(self._childuserids)
                    institutionFilter = "&institutionFilter[]=".join(self._institutionProfiles)
                    get_payload = '/relatedweekplan/all?currentWeekNumber='+week+'&userProfile=guardian&childFilter[]='+childFilter+'&institutionFilter[]='+institutionFilter
                    
                    mock_meebook = 0
                    if mock_meebook == 1:
                        _LOGGER.warning("Using mock data for Meebook ugeplaner.")
                        mock_meebook = '[{"id":490000,"name":"Emilie efternavn","unilogin":"lud...","weekPlan":[{"date":"mandag 28. nov.","tasks":[{"id":3069630,"type":"comment","author":"Met...","group":"3.a - ugeplan","pill":"Ingen fag tilknyttet","content":"I denne uge er der omlagt uge p\u00e5 hele skolen.\n\nMandag har vi \nKlippeklistredag:\n\nMan m\u00e5 gerne have nissehuer p\u00e5 :)\n\nMedbring gerne en god saks, limstift, skabeloner mm. \n\nB\u00f8rnene skal ogs\u00e5 medbringe et vasket syltet\u00f8jsglas eller lign., som vi skal male p\u00e5. S\u00f8rg gerne for at der ikke er m\u00e6rker p\u00e5:-)\n\n1. lektion: Morgenb\u00e5nd med l\u00e6sning/opgaver\n\n2. lektion: \nVi laver f\u00e6lles julenisser efter en bestemt skabelon.\n\n3. - 5. lektion: \nVi julehygger med musik og kreative projekter. Vi pynter vores f\u00e6lles juletr\u00e6, og synger julesange. \n\n6. lektion:\nAfslutning og oprydning.","editUrl":"https://app.meebook.com//arsplaner/dlap//956783//202248"}]},{"date":"tirsdag 29. nov.","tasks":[{"id":3069630,"type":"comment","author":"Met...","group":"3.a - ugeplan","pill":"Ingen fag tilknyttet","content":"Omlagt uge:\n\n1. lektion\nMorgenb\u00e5nd med l\u00e6sning og opgaver.\n\n2. lektion\nVi starter p\u00e5 storylineforl\u00f8b om jul. Vi taler om nisser og danner nissefamilier i klassen.\n\n3.-5. lektion\nVi lave et juleprojekt med filt...\n\n6. lektion\nVi arbejder med en kreativ opgave om v\u00e5benskold.","editUrl":"https://app.meebook.com//arsplaner/dlap//956783//202248"}]},{"date":"onsdag 30. nov.","tasks":[{"id":3069630,"type":"comment","author":"Met...","group":"3.a - ugeplan","pill":"Ingen fag tilknyttet","content":"Omlagt uge:\n\n1. -2. lektion\nVi skal til foredrag med SOS B\u00f8rnebyerne om omvendt julekalender.\n\n3-4. lektion\nVi skriver nissehistorier om nissefamilierne.\n\n5.-6. lektion\nVi laver jule-postel\u00f8b, hvor posterne skal l\u00e6ses med en kodel\u00e6ser.","editUrl":"https://app.meebook.com//arsplaner/dlap//956783//202248"}]},{"date":"torsdag 1. dec.","tasks":[{"id":3069630,"type":"comment","author":"Met...","group":"3.a - ugeplan","pill":"Ingen fag tilknyttet","content":"Omlagt uge:\n\n1. lektion\nMorgenb\u00e5nd med l\u00e6sning og opgaver. \nVi arbejder med l\u00e6s og forst\u00e5 i en julehistorie.\n\n2.-5. lektion\nVi skal arbejde med et kreativt juleprojekt, hvor der laves huse til nisserne.\n\n6. lektion\nSe SOS b\u00f8rnebyernes julekalender og afrunding af dagen.","editUrl":"https://app.meebook.com//arsplaner/dlap//956783//202248"}]},{"date":"fredag 2. dec.","tasks":[{"id":3069630,"type":"comment","author":"Met...","group":"3.a - ugeplan","pill":"Ingen fag tilknyttet","content":"1. lektion\nMorgenb\u00e5nd med l\u00e6sning og opgaver samt julehygge, hvor vi l\u00e6ser julehistorie \n\n2. lektion:\nVi skal lave et julerim og skrive det ind p\u00e5 en flot julenisse samt tegne nissen. \n\n3.-4. lektion\nVi skal lave jule-postel\u00f8b p\u00e5 skolen. \n\n5.. lektion\nVi skal l\u00f8se et hemmeligt kodebrev ved hj\u00e6lp af en kodel\u00e6ser. \n\nVi evaluerer og afrunder ugen.","editUrl":"https://app.meebook.com//arsplaner/dlap//956783//202248"}]}]},{"id":630000,"name":"Ann...","unilogin":"ann...","weekPlan":[{"date":"mandag 28. nov.","tasks":[{"id":3090189,"type":"comment","author":"May...","group":"0C (22/23)","pill":"B\u00f8rnehaveklasse, B\u00f8rnehaveklassen, Dansk, Matematik","content":"I dag skal vi h\u00f8re om jul i Norge og lave Norsk julepynt.\nEfter 12 pausen skal vi h\u00f8re om julen i Danmark f\u00f8r juletr\u00e6et og andestegen.\nVi skal farvel\u00e6gge g\u00e5rdnisserne der passede p\u00e5 g\u00e5rdene i gamle dage.","editUrl":"https://app.meebook.com//arsplaner/dlap//899210//202248"}]},{"date":"tirsdag 29. nov.","tasks":[{"id":3090189,"type":"comment","author":"May...","group":"0C (22/23)","pill":"B\u00f8rnehaveklasse, B\u00f8rnehaveklassen, Dansk, Matematik","content":"I dag skal vi arbejde med julen i Gr\u00f8nland og lave gr\u00f8nlandske julehuse.\nEfter 12 pausen skal vi h\u00f8re om JUletr\u00e6et der flytter ind i de danske stuer. Vi skal tale om hvor det stammer fra og hvad der var p\u00e5 juletr\u00e6et i gamle dage . Blandt andet den spiselige pynt.\nVi taler om Peters jul og at der ikke altid har v\u00e6ret en stjerne i toppen. Vi klipper storke til juletr\u00e6stoppen","editUrl":"https://app.meebook.com//arsplaner/dlap//899210//202248"}]},{"date":"onsdag 30. nov.","tasks":[{"id":3090189,"type":"comment","author":"May...","group":"0C (22/23)","pill":"B\u00f8rnehaveklasse, B\u00f8rnehaveklassen, Dansk, Matematik","content":"I dag st\u00e5r den p\u00e5 Jul i Finland og finske juletraditioner. Vi klipper finske julestjerner.\nEfter pausen skal vi arbejde videre med jul og julepynt gennem tiden i dk. \nVi skal tale om hvorfor der er flag, trompeter og trommer p\u00e5 tr\u00e6et (krigen i 1864) og vi skal lave gammeldags silkeroser og musetrapper til tr\u00e6et","editUrl":"https://app.meebook.com//arsplaner/dlap//899210//202248"}]},{"date":"torsdag 1. dec.","tasks":[{"id":3090189,"type":"comment","author":"May...","group":"0C (22/23)","pill":"B\u00f8rnehaveklasse, B\u00f8rnehaveklassen, Dansk, Matematik","content":"I dag skal vi p\u00e5 en juletur med hygge og posl\u00f8b til trylleskoven \nBussen k\u00f8rer os derud kl 10 og vi er senest tilbage n\u00e5r skoledagen slutter .\nHusk at f\u00e5 varmt praktisk t\u00f8j p\u00e5 og en turtaske med en let tilg\u00e6ngelig madpakke der kan spises i det fri. Regnbukser eller overtr\u00e6ksbukser s\u00e5 man kan sidde p\u00e5 jorden.","editUrl":"https://app.meebook.com//arsplaner/dlap//899210//202248"}]},{"date":"fredag 2. dec.","tasks":[{"id":3090189,"type":"comment","author":"May...","group":"0C (22/23)","pill":"B\u00f8rnehaveklasse, B\u00f8rnehaveklassen, Dansk, Matematik","content":"Klippe/ klistre dag .\nHusk at tage lim, saks og kaffe m.m., kop og tallerkner med hjemmefra. Hvis i tager kage med er det til en buffet i klassen.","editUrl":"https://app.meebook.com//arsplaner/dlap//899210//202248"}]}]}]'
                        data = json.loads(mock_meebook, strict=False)
                    else:
                        response = requests.get(MEEBOOK_API + get_payload, headers=headers, verify=True)
                        data = json.loads(response.text, strict=False)
                        #_LOGGER.debug("Meebook ugeplan raw response from week "+week+": "+str(response.text))
                    
                    for person in data:
                        _LOGGER.debug("Meebook ugeplan for "+person["name"])
                        ugep = ''
                        ugeplan = person["weekPlan"]
                        for day in ugeplan:
                            ugep = ugep+"<h3>"+day["date"]+"</h3>"
                            if len(day["tasks"]) > 0:
                                for task in day["tasks"]:
                                    if not task["pill"] == "Ingen fag tilknyttet":
                                        ugep = ugep+"<b>"+task["pill"]+"</b><br>"
                                    ugep = ugep+task["author"]+"<br><br>"
                                    content = re.sub(r"([0-9]+)(\.)", r"\1\.", task["content"])
                                    ugep = ugep+content+"<br><br>"
                            else:
                                ugep = ugep+"-"
                        try:
                            name = person["name"].split()[0]
                        except:
                            name = person["name"]
                        if thisnext == "this":
                            self.ugep_attr[name] = ugep
                        elif thisnext == "next":
                            self.ugepnext_attr[name] = ugep

            now = datetime.datetime.now() + datetime.timedelta(weeks=1)
            thisweek = datetime.datetime.now().strftime('%Y-W%W')
            nextweek = now.strftime("%Y-W%W")
            ugeplan(thisweek,"this")
            ugeplan(nextweek,"next")
            #_LOGGER.debug("End result of ugeplan object: "+str(self.ugep_attr))
        # End of Ugeplaner
        return True