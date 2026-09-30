package io.github.jatinmangla.jarvis

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.github.jatinmangla.jarvis.net.Pairing
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * App storage. Secrets (the device token + server address, cached reminder texts) are
 * encrypted with an AES-256-GCM key that lives in the Android Keystore (it never leaves the
 * phone's secure hardware and is not backed up). Harmless settings are stored plainly.
 */
class SecureStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    private companion object {
        const val KEY_ALIAS = "jarvis-storage"
        const val GCM_TAG_BITS = 128
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    @Synchronized
    private fun putSecret(name: String, value: String?) {
        if (value == null) {
            prefs.edit().remove(name).apply()
            return
        }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val sealed = cipher.iv + cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        prefs.edit().putString(name, Base64.encodeToString(sealed, Base64.NO_WRAP)).apply()
    }

    @Synchronized
    private fun getSecret(name: String): String? {
        val stored = prefs.getString(name, null) ?: return null
        return try {
            val sealed = Base64.decode(stored, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(GCM_TAG_BITS, sealed, 0, 12))
            String(cipher.doFinal(sealed, 12, sealed.size - 12), Charsets.UTF_8)
        } catch (e: GeneralSecurityException) {
            null // key gone (e.g. app data restored elsewhere): treat as not stored
        } catch (e: IllegalArgumentException) {
            null
        }
    }

    var pairing: Pairing?
        get() = Pairing.fromJson(getSecret("pairing"))
        set(value) = putSecret("pairing", value?.toJson())

    /** The reminders the phone rings for (JSON array), encrypted. */
    var remindersJson: String?
        get() = getSecret("reminders")
        set(value) = putSecret("reminders", value)

    var remindersFingerprint: String?
        get() = prefs.getString("reminders_fp", null)
        set(value) = prefs.edit().putString("reminders_fp", value).apply()

    var whisper: Boolean
        get() = prefs.getBoolean("whisper", false)
        set(value) = prefs.edit().putBoolean("whisper", value).apply()

    var startUrl: String?
        get() = prefs.getString("start_url", null)
        set(value) = prefs.edit().putString("start_url", value).apply()

    var lastUpdateCheck: Long
        get() = prefs.getLong("update_checked", 0)
        set(value) = prefs.edit().putLong("update_checked", value).apply()

    var scheduledAlarmIds: Set<String>
        get() = prefs.getStringSet("alarm_ids", emptySet()) ?: emptySet()
        set(value) = prefs.edit().putStringSet("alarm_ids", value).apply()

    /** Unpair: the token and everything cached from Jarvis are removed from this phone. */
    fun forget() {
        prefs.edit().remove("pairing").remove("reminders").remove("reminders_fp").apply()
    }
}
