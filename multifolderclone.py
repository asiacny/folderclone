from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from urllib3.exceptions import ProtocolError
from googleapiclient.discovery import build
from argparse import ArgumentParser
from httplib2shim import patch
from glob import glob
import time,threading,json,socket

account_count = 0
dtu = 1
retry = []
drive = []
threads = None
bad_drives = []

error_codes = {
    'dailyLimitExceeded': True,
    'userRateLimitExceeded': True,
    'rateLimitExceeded': True,
    'sharingRateLimitExceeded': True,
    'appNotAuthorizedToFile': True,
    'insufficientFilePermissions': True,
    'domainPolicy': True,
    'backendError': True,
    'internalError': True,
    'badRequest': False,
    'invalidSharingRequest': False,
    'authError': False,
    'notFound': False
}

patch()

def log(*l):
    global debug
    if debug:
        for i in l:
            print(i)

def apicall(request,sleep_time=1,max_retries=3):
    global error_codes
    
    resp = None
    tries = 0

    while True:
        tries += 1
        if tries > max_retries:
            return None
        try:
            resp = request.execute()
            if tries > 1:
                log('Successfully retried')
        except HttpError as error:
            log(error)
            try:
                error_details = json.loads(error.content.decode("utf-8"))
            except json.decoder.JSONDecodeError:
                time.sleep(sleep_time)
                continue
            reason = error_details["error"]["errors"][0]["reason"]
            if reason == 'userRateLimitExceeded':
                return False
            elif error_codes[reason]:
                time.sleep(sleep_time)
                continue
            else:
                return None
        except (socket.error, ProtocolError):
            time.sleep(sleep_time)
            continue
        else:
            return resp

def ls(parent, searchTerms=""):
    files = []
    
    resp = apicall(
        drive[0].files().list(
            q="'" + parent + "' in parents" + searchTerms,
            fields='files(md5Checksum,id,name),nextPageToken',
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        )
    )
    files += resp["files"]

    while "nextPageToken" in resp:
        resp = apicall(
            drive[0].files().list(
                q="'" + parent + "' in parents" + searchTerms,
                fields='files(md5Checksum,id,name),nextPageToken',
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=resp["nextPageToken"]
            )
        )
        files += resp["files"]
    return files

def lsd(parent):
    return ls(
        parent,
        searchTerms=" and mimeType contains 'application/vnd.google-apps.folder'"
    )

def lsf(parent):
    return ls(
        parent,
        searchTerms=" and not mimeType contains 'application/vnd.google-apps.folder'"
    )

def copy(driv, source, dest):
    global bad_drives
    global retry
    if apicall(driv.files().copy(fileId=source, body={"parents": [dest]}, supportsAllDrives=True)) == False:
        bad_drives.append(driv)
        retry.append((source,dest))
    threads.release()

def rcopy(drive, dtu, source, dest, sname, pre, width):
    global threads
    global retry
    global bad_drives

    pres = pre
    files_source = lsf(source)
    files_dest = lsf(dest)
    folders_source = lsd(source)
    folders_dest = lsd(dest)
    files_to_copy = []
    files_source_id = []
    files_dest_id = []

    fs = len(folders_source) - 1

    folders_copied = {}
    for file in files_source:
        files_source_id.append(dict(file))
        file.pop('id')
    for file in files_dest:
        files_dest_id.append(dict(file))
        file.pop('id')

    i = 0
    while len(files_source) > i:
        if files_source[i] not in files_dest:
            files_to_copy.append(files_source_id[i])
        i += 1
    for i in retry:
        threads.acquire()
        thread = threading.Thread(
            target=copy,
            args=(
                drive[dtu],
                i[0],
                i[1]
            )
        )
        thread.start()
        dtu += 1
        if dtu > len(drive) - 1:
            dtu = 1
    retry = []
    if len(files_to_copy) > 0:
        for file in files_to_copy:
            threads.acquire()
            thread = threading.Thread(
                target=copy,
                args=(
                    drive[dtu],
                    file['id'],
                    dest
                )
            )
            thread.start()
            dtu += 1
            if dtu > len(drive) - 1:
                dtu = 1
        print(pres + sname + ' | Synced')
    elif len(files_source) > 0 and len(files_source) <= len(files_dest):
        print(pres + sname + ' | Up to date')
    else:
        print(pres + sname)
    log(len(bad_drives),bad_drives)
    log(len(drive))
    for i in bad_drives:
        if i in drive:
            drive.remove(i)
    bad_drives = []
    if len(drive) == 1:
        print('Out of SAs.')
        return

    for i in folders_dest:
        folders_copied[i['name']] = i['id']
    
    s = 0
    for folder in folders_source:
        if s == fs:
            nstu = pre.replace("├" + "─" * width + " ", "│" + " " * width + " ").replace("└" + "─" * width + " ", "  " + " " * width) + "└" + "─" * width + " "
        else:
            nstu = pre.replace("├" + "─" * width + " ", "│" + " " * width + " ").replace("└" + "─" * width + " ", "  " + " " * width) + "├" + "─" * width + " "
        if folder['name'] not in folders_copied.keys():
            folder_id = apicall(
                drive[0].files().create(
                    body={
                        "name": folder["name"],
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": [dest]
                    },
                    supportsAllDrives=True
                )
            )['id']
        else:
            folder_id = folders_copied[folder['name']]
        drive = rcopy(
            drive,
            dtu,
            folder["id"],
            folder_id,
            folder["name"].replace('%', "%%"),
            nstu,
            width
        )
        s += 1
    return drive

def multifolderclone(source=None, dest=None, path='accounts', width=2, thread_count=None):
    global account_count
    global drive
    global threads

    stt = time.time()
    accounts = glob(path + '/*.json')

    check = build("drive", "v3", credentials=Credentials.from_service_account_file(accounts[0]))
    try:
        root_dir = check.files().get(fileId=source, supportsAllDrives=True).execute()['name']
    except HttpError:
        print('Source folder cannot be read or is invalid.')
        exit(0)
    try:
        dest_dir = check.files().get(fileId=dest, supportsAllDrives=True).execute()['name']
    except HttpError:
        print('Destination folder cannot be read or is invalid.')
        exit(0)

    print('Copy from ' + root_dir + ' to ' + dest_dir + '.')
    print('View set to tree (' + str(width) + ').')

    print("Creating %d Drive Services" % len(accounts))
    for account in accounts:
        account_count += 1
        credentials = Credentials.from_service_account_file(account, scopes=[
            "https://www.googleapis.com/auth/drive"
        ])
        drive.append(build("drive", "v3", credentials=credentials))
    if thread_count is not None and thread_count <= account_count:
        threads = threading.BoundedSemaphore(thread_count)
    else:
        threads = threading.BoundedSemaphore(account_count)

    print('BoundedSemaphore with %d threads' % account_count)

    try:
        rcopy(drive, 1, source, dest, root_dir, "", width)
    except KeyboardInterrupt:
        print('Quitting')
        exit(0)

    print('Complete.')
    hours, rem = divmod((time.time() - stt), 3600)
    minutes, sec = divmod(rem, 60)
    print("Elapsed Time:\n{:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), sec))

def main():
    global debug
    parse = ArgumentParser(description='A tool intended to copy large files from one folder to another.')
    parse.add_argument('--width', '-w', type=int, default=2, help='Set the width of the view option.')
    parse.add_argument('--path', '-p', default='accounts', help='Specify an alternative path to the service accounts.')
    parse.add_argument('--debug-mode',default=False,action='store_true',help='Completely verbose.')
    parse.add_argument('--threads', type=int, default=None,help='Specify a different thread count. Cannot be greater than the amount of service accounts available.')
    parsereq = parse.add_argument_group('required arguments')
    parsereq.add_argument('--source-id', '-s',help='The source ID of the folder to copy.',required=True)
    parsereq.add_argument('--destination-id', '-d',help='The destination ID of the folder to copy to.',required=True)
    args = parse.parse_args()
    debug = args.debug_mode
    multifolderclone(
        source=args.source_id,
        dest=args.destination_id,
        path=args.path,
        width=args.width,
        thread_count=args.threads
    )

if __name__ == '__main__':
    main()
    
