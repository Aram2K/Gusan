package com.aram2k.gusan.core

import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthTest {
    private val ok = """{"access_token":"a.b.c","expires_in":3600,"refresh_token":"r1","user":{"id":"u1","email":"me@example.com"}}"""

    @Test fun passwordSignInStoresTheSession() {
        val storage = MemorySessionStorage()
        val transport = FakeTransport { json(200, ok) }
        val session = AuthClient(storage, transport) { 1_000 }.signIn(" me@example.com ", "pw")
        assertEquals("a.b.c", session.accessToken)
        assertEquals(1_000 + 3_540_000, session.expiresAtMs)
        assertEquals(session, storage.read())
        val request = transport.requests.single()
        assertEquals(Backend.BASE_URL + "/auth/v1/token?grant_type=password", request.url)
        assertEquals(Backend.PUBLISHABLE_KEY, request.headers["apikey"])
        assertEquals("me@example.com", request.json()["email"]!!.jsonPrimitive.content)
    }

    @Test fun signInSaysWhatWentWrongInPlainWords() {
        fun message(body: String) = try {
            AuthClient(MemorySessionStorage(), FakeTransport { json(400, body) }).signIn("a@b.c", "x"); ""
        } catch (e: GusanException) { e.message }
        assertEquals("Incorrect email or password.", message("""{"error_description":"Invalid login credentials"}"""))
        assertEquals("Please verify your email first - check your inbox.", message("""{"msg":"Email not confirmed"}"""))
        assertEquals("Sign-in failed. Try again.", message("not json"))
    }

    @Test fun aNewAccountIsSignedInAtOnceOrWaitsForItsEmail() {
        val storage = MemorySessionStorage()
        val verify = AuthClient(storage, FakeTransport { json(200, """{"id":"u1","email":"me@example.com"}""") })
        assertNull(verify.signUp("me@example.com", "secret1"))
        assertNull(storage.read())
        val direct = AuthClient(storage, FakeTransport { json(200, """{"session":$ok}""") })
        assertEquals("a.b.c", direct.signUp("me@example.com", "secret1")!!.accessToken)
        assertEquals("r1", storage.read()!!.refreshToken)
        fun message(body: String) = try {
            AuthClient(MemorySessionStorage(), FakeTransport { json(422, body) }).signUp("a@b.c", "x"); ""
        } catch (e: GusanException) { e.message }
        assertEquals("There is already an account with this email. Sign in instead.", message("""{"msg":"User already registered"}"""))
        assertEquals("Choose a longer password (at least 6 characters).", message("""{"msg":"Password should be at least 6 characters"}"""))
        assertEquals("Couldn't create the account. Try again.", message("oops"))
    }

    @Test fun googleOnThePhoneSignsInWithItsIdToken() {
        val storage = MemorySessionStorage()
        val transport = FakeTransport { json(200, ok) }
        AuthClient(storage, transport).signInWithIdToken("google.jwt", "raw-nonce")
        val request = transport.requests.single()
        assertEquals(Backend.BASE_URL + "/auth/v1/token?grant_type=id_token", request.url)
        assertEquals("google", request.json()["provider"]!!.jsonPrimitive.content)
        assertEquals("raw-nonce", request.json()["nonce"]!!.jsonPrimitive.content)
        assertEquals("a.b.c", storage.read()!!.accessToken)
        // SHA-256("abc")
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", AuthClient.hashNonce("abc"))
    }

    @Test fun aFreshSessionIsNotRenewed() {
        val storage = MemorySessionStorage(Session("a", 10_000, "r"))
        val transport = FakeTransport { error("no request expected") }
        assertEquals("a", AuthClient(storage, transport) { 5_000 }.accessToken())
    }

    @Test fun anExpiredSessionIsRenewedAndKeepsItsRefreshTokenWhenNoneComesBack() {
        val storage = MemorySessionStorage(Session("old", 1_000, "r1", "u1", "me@example.com"))
        val transport = FakeTransport { json(200, """{"access_token":"new","expires_in":100}""") }
        val session = AuthClient(storage, transport) { 5_000 }.refresh()!!
        assertEquals("new", session.accessToken)
        assertEquals("r1", session.refreshToken)
        assertEquals("r1", transport.requests.single().json()["refresh_token"]!!.jsonPrimitive.content)
    }

    @Test fun aDeadRefreshTokenSignsOut_aServerHiccupDoesNot() {
        val dead = MemorySessionStorage(Session("old", 0, "r1"))
        assertEquals(GusanException.Kind.UNAUTHORIZED, kindOf {
            AuthClient(dead, FakeTransport { json(400, """{"error_code":"refresh_token_not_found"}""") }) { 5 }.refresh()
        })
        assertNull(dead.read())

        val kept = MemorySessionStorage(Session("old", 0, "r1"))
        assertEquals(GusanException.Kind.FAILED, kindOf {
            AuthClient(kept, FakeTransport { json(500, "oops") }) { 5 }.refresh()
        })
        assertEquals("r1", kept.read()!!.refreshToken)
    }

    @Test fun nobodySignedIn() {
        assertEquals(GusanException.Kind.NOT_SIGNED_IN, kindOf {
            AuthClient(MemorySessionStorage(), FakeTransport { error("no request expected") }).accessToken()
        })
    }

    @Test fun entitlement() {
        fun read(body: String) = AuthClient(MemorySessionStorage(), FakeTransport { json(200, body) }).entitlement("t")
        assertEquals(Entitlement(true, "pro_monthly"), read("""[{"is_pro":true,"plan":"pro_monthly"}]"""))
        assertEquals(Entitlement(false, null), read("[]"))
        assertEquals(Entitlement(false, null), read("""[{"is_pro":null}]"""))
    }

    @Test fun pkceChallengeIsTheKnownVector() {
        // RFC 7636, appendix B
        assertEquals("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", Pkce("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk").challenge)
        val url = AuthClient.authorizeUrl(Pkce("v"))
        assertTrue(url.contains("provider=google") && url.contains("redirect_to=gusan%3A%2F%2Fauth-callback"))
        assertFalse(Pkce.generate().verifier.contains("="))
    }

    @Test fun sessionSurvivesBeingStoredAsText() {
        val s = Session("a", 12, "r", "u", "e@x.y")
        assertEquals(s, Session.fromJson(s.toJson()))
        assertEquals(Session("a", 12, "r"), Session.fromJson(Session("a", 12, "r").toJson()))
        assertNull(Session.fromJson("{}"))
    }

    @Test fun signOutForgetsTheSessionEvenOffline() {
        val storage = MemorySessionStorage(Session("a", 0, "r"))
        AuthClient(storage, FakeTransport { throw java.io.IOException("offline") }).signOut()
        assertNull(storage.read())
    }
}
