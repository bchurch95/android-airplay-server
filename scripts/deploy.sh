#!/bin/bash
set -e
cd /root/android-airplay-server

echo '[*] Building debug APK...'
./gradlew assembleDebug

APK='/root/android-airplay-server/app/build/outputs/apk/debug/app-debug.apk'
echo "[+] Build complete: $APK"

for DEV in '192.168.2.162:5555' '192.168.77.140:5555'; do
    echo "[*] Deploying to $DEV..."
    adb connect "$DEV" >/dev/null 2>&1 || true
    adb -s "$DEV" install -r "$APK" || (
        echo "[!] Reinstalling on $DEV..."
        adb -s "$DEV" uninstall io.github.jqssun.airplay >/dev/null 2>&1 || true
        adb -s "$DEV" install "$APK"
    )
    adb -s "$DEV" shell am force-stop io.github.jqssun.airplay >/dev/null 2>&1 || true
    adb -s "$DEV" shell am start -n io.github.jqssun.airplay/.MainActivity >/dev/null 2>&1
    echo "[+] Done with $DEV!"
done
echo '[+] All deployments complete!'
