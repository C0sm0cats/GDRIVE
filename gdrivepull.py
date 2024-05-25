import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import pytz

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ID of the folder in Google Drive from which you want to retrieve information
FOLDER_ID = "your_folder_id"

# Path where you want to download files from Google Drive
DOWNLOAD_PATH = '/your_download_path/'


def download_files_recursively(service, folder_id, local_path):
    """Downloads recursively all content from a specific folder on Google Drive."""
    results = service.files().list(q=f"'{folder_id}' in parents", fields="files(id, name, mimeType)").execute()
    items = results.get("files", [])

    if not items:
        print("No files found in this folder.")
        return

    for item in items:
        item_name = item["name"]
        item_id = item["id"]
        item_type = item["mimeType"]

        # If it's a folder, recursively download its content
        if item_type == "application/vnd.google-apps.folder":
            new_local_path = os.path.join(local_path, item_name)
            os.makedirs(new_local_path, exist_ok=True)
            print(f"Downloading content of folder: {new_local_path}")
            download_files_recursively(service, item_id, new_local_path)
        else:
            # Download the file into the specified local folder
            local_file_path = os.path.join(local_path, item_name)
            print(f"Downloading file: {local_file_path}")
            request = service.files().get_media(fileId=item_id)
            with open(local_file_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    print(f"Downloading {item_name} at {int(status.progress() * 100)}%.")


def convert_expiry_to_paris_time(expiry_utc):
    utc_timezone = pytz.utc
    paris_timezone = pytz.timezone('Europe/Paris')
    expiry_utc = utc_timezone.localize(expiry_utc)
    expiry_paris = expiry_utc.astimezone(paris_timezone)
    return expiry_paris


def main():
    """Recursively retrieves all content from a specific folder on Google Drive."""
    creds = None
    token_path = "token.json"

    # Check if the token.json file exists and contains valid credentials
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # If no valid credentials are found, allow the user to log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                print("Token refreshed:")
                print(f"Access Token: {creds.token}")
                print(f"Refresh Token: {creds.refresh_token}")
                print("Expiry:", convert_expiry_to_paris_time(creds.expiry))
            except Exception as e:
                print(f"Error refreshing token: {e}")
                flow = InstalledAppFlow.from_client_secrets_file(
                    "credentials.json", SCOPES
                )
                creds = flow.run_local_server(port=0)
                print("New authorization:")
                print(f"Access Token: {creds.token}")
                print(f"Refresh Token: {creds.refresh_token}")
                print("Expiry:", convert_expiry_to_paris_time(creds.expiry))
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
            print("New authorization:")
            print(f"Access Token: {creds.token}")
            print(f"Refresh Token: {creds.refresh_token}")
            print("Expiry:", convert_expiry_to_paris_time(creds.expiry))

        # Save the credentials for the next execution
        with open(token_path, "w") as token:
            token.write(creds.to_json())
    else:
        print("Existing token:")
        print(f"Access Token: {creds.token}")
        print(f"Refresh Token: {creds.refresh_token}")
        print("Expiry:", convert_expiry_to_paris_time(creds.expiry))

    try:
        # Initialize the Google Drive service
        service = build("drive", "v3", credentials=creds)

        # Recursively download all content from the specified folder
        download_files_recursively(service, FOLDER_ID, DOWNLOAD_PATH)
    except HttpError as error:
        # Handle errors from the Google Drive API
        print(f"An error occurred: {error}")
    # finally:
    #     # Remove the token.json file at the end of execution
    #     if os.path.exists("/token.json"):
    #         os.remove("/token.json")


if __name__ == "__main__":
    main()
