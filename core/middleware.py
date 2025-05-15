from django.contrib.auth.middleware import get_user
from django.utils.functional import SimpleLazyObject
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from django.http import JsonResponse
from rest_framework import status


class VerificationMiddleWare:
    def __init__(self, get_response) -> None:
        self.get_reseponse = get_response

    def __call__(self, request):

        if getattr(request.user, 'is_authenticated', None):
            if not request.user.email_verified or not request.user.phone_verified:
                return JsonResponse(
                    {
                        "status": "failed",
                        "message:": "please verify your email and phone.",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
        response = self.get_reseponse(request)

        return response


class AuthenticationMiddlewareJWT:
    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def __call__(self, request):
        user = SimpleLazyObject(lambda: self.__class__.get_jwt_user(request))
        if isinstance(user, InvalidToken):
            return JsonResponse(
                {
                    "status": "failed",
                    "message:": "Either token is invalid or expired or not present in cookie",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        request.user = user
        return self.get_response(request)

    @staticmethod
    def get_jwt_user(request):
        user = get_user(request)
        if user.is_authenticated:
            return user
        try:
            jwt_user = JWTAuthentication().authenticate(Request(request))
            if jwt_user is not None:
                return jwt_user[0]
        except Exception as e:
            return e
        return user
