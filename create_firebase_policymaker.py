"""Create a Firebase Authentication user with the policymaker custom claim."""

import getpass
import os

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import auth


def firebase_app():
    try:
        return firebase_admin.get_app()
    except ValueError:
        options = {}
        database_url = os.environ.get("FIREBASE_DATABASE_URL")
        project_id = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if database_url:
            options["databaseURL"] = database_url
        if project_id:
            options["projectId"] = project_id
        return firebase_admin.initialize_app(options=options)


def main():
    load_dotenv()
    app = firebase_app()
    email = input("Policymaker email: ").strip().lower()
    password = getpass.getpass("Temporary password (at least 8 characters): ")
    if "@" not in email or len(password) < 8:
        raise SystemExit("Provide a valid email and a password with at least 8 characters.")
    try:
        user = auth.get_user_by_email(email, app=app)
        auth.update_user(user.uid, password=password, app=app)
        action = "updated"
    except auth.UserNotFoundError:
        user = auth.create_user(email=email, password=password, email_verified=True, app=app)
        action = "created"
    auth.set_custom_user_claims(user.uid, {"policymaker": True}, app=app)
    print("Firebase policymaker {}: {}".format(action, email))


if __name__ == "__main__":
    main()
