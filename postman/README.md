# Postman collection

`lecturekit.postman_collection.json` has one request for every route of the API (39 requests in 10 folders: System, Auth, Courses, Modules, Sessions, Processing, Jobs, Results, Live Recording, Account). Each request has a description (purpose, authentication, parameters, possible responses) and test scripts that check the answer.

## Setup

1. Import the collection and `lecturekit.local.postman_environment.json`, and select the environment **LectureKit Local**.
2. `baseUrl` in the environment is `https://localhost:8443` (the local Docker stack); for a real server use `https://your-domain`.
3. Settings, General: turn **SSL certificate verification** off (the local certificate is self-signed; not needed for a real domain).
4. Settings, General: set the **working directory** to `postman/sample-files`, so the upload requests find their files.

## Email confirmation

New accounts must confirm their email with a 6-digit code before they can log in; the code is only in the email, never in an API reply, and a welcome email follows once it is confirmed.

1. Run **Register** (to use a fixed address, add the environment variable `email`).
2. Open the email (Mailtrap inbox when testing) and copy the 6-digit code into the environment variable `otpCode`.
3. Run **Verify Email**, then **Login**. If Verify Email is run with `otpCode` empty it is skipped.

Login With Google sends a fake token and checks that it is refused; the real Google flow needs a browser and is tested from the web app.

## Run

- **Whole collection:** Runner, in the order shown, after Register and Verify Email (see above): the Runner cannot pause for the code, so run the Auth folder's Register, then set `otpCode`, then run the rest. Process Session and Finish Live Session wait for the background job, so the run takes about a minute and needs the Anthropic API key on the server. Delete Account is the last request: it removes the account you logged in with, so the next run starts again with Register and a new code.
- **One request:** run Register, Verify Email and Login once, then Create Course, Create Module and Create Session set `courseId`, `moduleId` and `sessionId` for the requests that need them. The Delete requests, Retry Session and the live requests create their own throw-away data first, so they work on their own.

## Command line (Newman)

The code cannot be typed in a script, so register the account and confirm it with the admin command first (a fixed email, here `ci@example.com`), then run:

```bash
docker run --rm --network lecturekit_default -v "$PWD/postman:/etc/newman" postman/newman:latest \
  run lecturekit.postman_collection.json -e lecturekit.local.postman_environment.json \
  --env-var baseUrl=http://app:8000 --env-var email=ci@example.com \
  --working-dir /etc/newman/sample-files
```

## Files

`sample-files/` has a 51-second spoken recording (`sample-recording.m4a`), a slide deck (`sample-slides.pdf`) and a short piece for live recording (`chunk0.m4a`).

The collection is generated: edit `scripts/make_postman.py` and run `python scripts/make_postman.py`.
