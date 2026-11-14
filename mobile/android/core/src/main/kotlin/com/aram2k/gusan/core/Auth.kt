package com.aram2k.gusan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.net.URLEncoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

data class Session(
    val accessToken: String,
    val expiresAtMs: Long,
    val refreshToken: String,
    val userId: String? = null,
    val email: String? = null,
) {
    fun toJson(): String = buildJsonObject {
        put("access_token", accessToken)
        put("expires_at_ms", expiresAtMs)
        put("refresh_token", refreshToken)
        userId?.let { put("user_id", it) }
        email?.let { put("email", it) }
    }.toString()

    companion object {
        fun fromJson(text: String): Session? = runCatching {
            val o = Json.parseToJsonElement(text).jsonObject
            Session(
                accessToken = o.string("access_token")!!,
                expiresAtMs = (o["expires_at_ms"] as JsonPrimitive).content.toLong(),
                refreshToken = o.string("refresh_token")!!,
                userId = o.string("user_id"),
                email = o.string("email"),
            )
        }.getOrNull()
    }
}

interface SessionStorage {
    fun read(): Session?
    fun write(session: Session)
    fun clear()
}

class MemorySessionStorage(private var session: Session? = null) : SessionStorage {
    override fun read(): Session? = session
    override fun write(session: Session) { this.session = session }
    override fun clear() { session = null }
}

data class Entitlement(val isPro: Boolean, val plan: String?)

class Pkce(val verifier: String) {
    val challenge: String
        get() = base64Url(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray(Charsets.US_ASCII)))

    companion object {
        fun generate(random: SecureRandom = SecureRandom()): Pkce =
            Pkce(base64Url(ByteArray(32).also(random::nextBytes)))

        private fun base64Url(bytes: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }
}

internal fun JsonObject.string(key: String): String? =
    (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

internal fun parseObject(bytes: ByteArray): JsonObject? =
    runCatching { Json.parseToJsonElement(bytes.toString(Charsets.UTF_8)) as? JsonObject }.getOrNull()

class AuthClient(
    val storage: SessionStorage,
    private val transport: HttpTransport = UrlConnectionTransport(),
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val refreshLock = Any()

    private fun post(path: String, body: JsonObject = JsonObject(emptyMap()), token: String? = null): HttpResponse {
        val headers = buildMap {
            put("apikey", Backend.PUBLISHABLE_KEY)
            put("Content-Type", "application/json")
            if (token != null) put("Authorization", "Bearer $token")
        }
        return transport.sendOrThrow(
            HttpRequest(Backend.BASE_URL + path, headers, body.toString().toByteArray(), timeoutMs = 10_000),
        )
    }

    private fun signIn(grant: String, body: JsonObject): Session {
        val response = post("/auth/v1/token?grant_type=$grant", body)
        if (response.status != 200) {
            val message = if (grant == "password") signInMessage(response.body) else "Google sign-in failed. Try again."
            throw GusanException(GusanException.Kind.SIGN_IN_FAILED, message)
        }
        return decodeSession(response.body, null, now()).also(storage::write)
    }

    fun signIn(email: String, password: String): Session =
        signIn("password", buildJsonObject { put("email", email.trim()); put("password", password) })

    /**
     * Creates an account. Returns the session when the account can be used at once, or null when a
     * confirmation email was sent and the person has to click its link first.
     */
    fun signUp(email: String, password: String): Session? {
        val response = post("/auth/v1/signup", buildJsonObject { put("email", email.trim()); put("password", password) })
        if (response.status != 200 && response.status != 201) {
            throw GusanException(GusanException.Kind.SIGN_IN_FAILED, signUpMessage(response.body))
        }
        val o = parseObject(response.body) ?: throw GusanException(GusanException.Kind.INVALID_RESPONSE)
        val session = (o["session"] as? JsonObject) ?: o
        if (session.string("access_token").isNullOrEmpty()) return null
        return decodeSession(session.toString().toByteArray(), null, now()).also(storage::write)
    }

    /** Sends the email with a link to choose a new password. Says nothing about whether the address has an account. */
    fun sendPasswordReset(email: String) {
        post("/auth/v1/recover", buildJsonObject { put("email", email.trim()) })
    }

    fun exchange(code: String, pkce: Pkce): Session =
        signIn("pkce", buildJsonObject { put("auth_code", code); put("code_verifier", pkce.verifier) })

    /** The stored session, renewed first when it has run out. Null when nobody is signed in. */
    fun refresh(force: Boolean = false): Session? = synchronized(refreshLock) {
        val before = storage.read() ?: return null
        if (!force && before.expiresAtMs > now()) return before
        val response = post("/auth/v1/token?grant_type=refresh_token", buildJsonObject { put("refresh_token", before.refreshToken) })
        if (response.status != 200) {
            if (refreshTokenRejected(response.status, response.body)) {
                storage.clear()
                throw GusanException(GusanException.Kind.UNAUTHORIZED)
            }
            throw GusanException(GusanException.Kind.FAILED)
        }
        decodeSession(response.body, before.refreshToken, now()).also(storage::write)
    }

    /** A token that is good right now, or NOT_SIGNED_IN / UNAUTHORIZED. */
    fun accessToken(): String =
        (refresh() ?: throw GusanException(GusanException.Kind.NOT_SIGNED_IN)).accessToken

    fun entitlement(accessToken: String): Entitlement {
        val response = post("/rest/v1/rpc/my_entitlement", token = accessToken)
        if (response.status == 401) throw GusanException(GusanException.Kind.UNAUTHORIZED)
        if (response.status != 200) throw GusanException(GusanException.Kind.FAILED)
        val rows = runCatching { Json.parseToJsonElement(response.text) as? JsonArray }.getOrNull()
            ?: throw GusanException(GusanException.Kind.INVALID_RESPONSE)
        val row = rows.firstOrNull() as? JsonObject ?: return Entitlement(false, null)
        return Entitlement((row["is_pro"] as? JsonPrimitive)?.booleanOrNull == true, row.string("plan"))
    }

    fun signOut() {
        val session = storage.read()
        storage.clear()
        if (session != null) runCatching { post("/auth/v1/logout", token = session.accessToken) }
    }

    companion object {
        fun authorizeUrl(pkce: Pkce): String =
            Backend.BASE_URL + "/auth/v1/authorize?provider=google" +
                "&redirect_to=" + URLEncoder.encode(Backend.AUTH_REDIRECT, "UTF-8") +
                "&code_challenge=" + pkce.challenge + "&code_challenge_method=s256"

        fun decodeSession(data: ByteArray, previousRefreshToken: String?, nowMs: Long): Session {
            val o = parseObject(data) ?: throw GusanException(GusanException.Kind.INVALID_RESPONSE)
            val access = o.string("access_token").orEmpty()
            val refresh = o.string("refresh_token") ?: previousRefreshToken.orEmpty()
            if (access.isEmpty() || refresh.isEmpty()) throw GusanException(GusanException.Kind.INVALID_RESPONSE)
            val expiresIn = (o["expires_in"] as? JsonPrimitive)?.doubleOrNull ?: 3600.0
            val user = o["user"] as? JsonObject
            val email = user?.string("email") ?: (user?.get("user_metadata") as? JsonObject)?.string("email")
            return Session(access, nowMs + ((expiresIn - 60) * 1000).toLong(), refresh, user?.string("id"), email)
        }

        /** Same wording as the desktop app (auth.py sign_in_email). */
        fun signInMessage(data: ByteArray): String {
            val o = parseObject(data)
            val message = listOf("msg", "error_description", "message", "error")
                .firstNotNullOfOrNull { key -> o?.string(key)?.takeIf { it.isNotEmpty() } }
                ?: "Sign-in failed. Try again."
            val low = message.lowercase()
            return when {
                "not confirmed" in low || "not been confirmed" in low -> "Please verify your email first - check your inbox."
                "invalid" in low || "credential" in low -> "Incorrect email or password."
                else -> message
            }
        }

        fun signUpMessage(data: ByteArray): String {
            val o = parseObject(data)
            val message = listOf("msg", "error_description", "message", "error")
                .firstNotNullOfOrNull { key -> o?.string(key)?.takeIf { it.isNotEmpty() } }
                ?: return "Couldn't create the account. Try again."
            val low = message.lowercase()
            return when {
                "already registered" in low || "already exists" in low -> "There is already an account with this email. Sign in instead."
                "password" in low && ("least" in low || "short" in low || "weak" in low) -> "Choose a longer password (at least 6 characters)."
                "valid email" in low || "invalid email" in low || "invalid format" in low -> "Enter a valid email address."
                else -> message
            }
        }

        fun refreshTokenRejected(status: Int, data: ByteArray): Boolean {
            if (status != 400 && status != 401) return false
            val dead = setOf("refresh_token_not_found", "refresh_token_already_used", "session_not_found", "session_expired", "invalid_grant")
            val o = parseObject(data)
            return listOf("error_code", "error").any { o?.string(it) in dead } ||
                "invalid refresh token" in data.toString(Charsets.UTF_8).lowercase()
        }
    }
}
