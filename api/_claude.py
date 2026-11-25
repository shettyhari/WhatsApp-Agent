"""Calling Claude (Anthropic) from the API, with error messages people can act on."""
import os

from _core import ApiError

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
# If a request is declined by the safety classifier, the API retries it on a fallback model
FALLBACK_ARGS = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def client(timeout):
    try:
        import anthropic
    except ImportError:
        raise ApiError(503, "The Anthropic SDK is not installed on the server. Run: pip install -r requirements.txt")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ApiError(503, "ANTHROPIC_API_KEY is not set on the server, so the AI can't run yet (see README.md).")
    return anthropic.Anthropic(timeout=timeout, max_retries=2)


def run(fn, what="the AI service"):
    """Run fn() (a Claude call) and turn SDK errors into ApiErrors."""
    import anthropic
    try:
        return fn()
    except anthropic.AuthenticationError:
        raise ApiError(503, "The Anthropic API key on the server was rejected. Check ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        raise ApiError(429, "The AI service is busy right now. Try again in a minute.")
    except anthropic.BadRequestError as e:
        print(f"[Claude] bad request ({what}):", getattr(e, "message", e))
        raise ApiError(400, f"The AI couldn't process {what}. If it's a file, check it opens correctly and isn't password-protected.")
    except anthropic.APIStatusError as e:
        print(f"[Claude] API error ({what}):", e.status_code, getattr(e, "message", e))
        raise ApiError(502, "The AI service returned an error. Try again shortly.")
    except anthropic.APIConnectionError:
        raise ApiError(502, "Couldn't reach the AI service. Check the server's internet connection.")
