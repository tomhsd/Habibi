# Habibi phone app

Lets your Habibi PC assistant control this Android phone, and puts the Habibi
remote (with microphone and speaker) in its own full-screen app.

## Get the APK — option 1: GitHub (automatic)
Every push to the Habibi repository builds the APK automatically. Download
**Habibi.apk** from the repository's **Releases → Latest build** page.

## Get the APK — option 2: Android Studio
1. Install Android Studio, choose **Open**, and select this `phone_app` folder.
2. Wait for "Gradle sync" to finish.
3. **Build → Build App Bundle(s) / APK(s) → Build APK(s)** → click **locate** → `app-debug.apk`.

## Install and pair
1. Copy `app-debug.apk` to the phone and open it (allow "install unknown apps" when asked).
2. On the PC, open Habibi → **REMOTE CONTROL**.
3. In the phone app, type the address shown under the QR code and the 6-letter key → **PAIR**.
4. Tap **⚙ SETUP** and allow the items you want Habibi to use.
