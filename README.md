# GDRIVE

This Python script facilitates the recursive download of folders and files from a specific folder on Google Drive.
It interacts with the Google Drive API to retrieve files and folders, downloading them to a specified local directory.

## Prerequisites

Before using this script, ensure you have the following prerequisites:
- Python installed on your system (version 3.6 or higher)
- Google Cloud Platform project with Google Drive API enabled
- OAuth 2.0 credentials file (`credentials.json`) obtained from the Google Cloud Console
- Dependencies installed (`google-auth`, `google-auth-oauthlib`, `google-api-python-client`, `pytz`)
- Google Drive folder ID from which you want to download subfolders and files
- Path to the directory where you want to download the files from Google Drive

For more detailed information on setting up and running an app that calls a Google Workspace API, visit the [Google Drive API Quickstart guide](https://developers.google.com/drive/api/quickstart/python).

## Usage

1. Clone this repository to your local machine.

    ```bash
    git clone https://github.com/C0sm0cats/GDRIVE.git
    ```

2. Replace the values of the variables `FOLDER_ID` and `DOWNLOAD_PATH` in the script (`gdrivepull.py`) with your desired Google Drive folder ID and download directory path, respectively.

3. Run the script.

    ```bash
    python gdrivepull.py
    ```

4. Follow the instructions in the terminal to authenticate with your Google account and start the recursive download process.

## Notes

- This script requires OAuth 2.0 credentials to access the Google Drive API. Ensure that you set up the credentials file (`credentials.json`) correctly.
- Make sure the specified download directory (`DOWNLOAD_PATH`) exists and has write permissions.