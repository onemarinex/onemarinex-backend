"""Do not cache company records or privileged review responses."""


async def company_private_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(
        (
            "/api/v1/shipping-company",
            "/api/v1/superadmin",
            "/api/v1/crew/welfare-feedback",
        )
    ):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
    return response
