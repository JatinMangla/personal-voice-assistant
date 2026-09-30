# Keep line numbers out of crash traces we never collect anyway; no extra keeps needed:
# OkHttp, Media3, Coil and ZXing ship their own consumer rules.
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
