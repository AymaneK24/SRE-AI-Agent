# Single sign-on

People can sign in with your company's identity provider through OpenID Connect (OIDC): Microsoft Entra ID, Google Workspace, Okta, Keycloak, and others. Owners set it up in **Organization > Single sign-on**.

## How it works

1. On the sign-in page, a person chooses **Sign in with SSO** and enters their work email.
2. The console finds the SSO setup whose **email domains** include that address's domain, and sends the person to your provider.
3. They sign in there, with your provider's two-factor rules.
4. The provider sends them back to the console's redirect address with a one-time code. The console exchanges it for their identity.
5. If the email is verified and in an allowed domain, they're signed in. Someone signing in for the first time gets an account with the role in **Role for new people**.

The console uses the authorization code flow with PKCE, a one-time `state` that expires after 10 minutes, and a `nonce`. It asks for the scopes `openid email profile`, and reads the person's `sub`, `email`, `email_verified`, and `name` from the provider's userinfo endpoint. After the first sign-in, the account is tied to the provider's `sub`, so a different identity with the same email can't take it over.

## The settings

| Field | What to enter |
|---|---|
| Turn on single sign-on | Tick when the rest is filled |
| Issuer address | Your provider's issuer. The console reads `ISSUER/.well-known/openid-configuration` |
| Client ID, client secret | From the app registration at your provider. The secret is encrypted |
| Email domains | Domains that may use it, such as `company.com, company.fr` |
| Role for new people | `viewer` or `engineer` |

The **redirect address** shown on the page is `RCC_PUBLIC_URL` followed by `/api/auth/sso/callback`, such as `https://rcc.company.com/api/auth/sso/callback`. Register it exactly at your provider.

## Microsoft Entra ID

```bash
TENANT=$(az account show --query tenantId -o tsv)
APP=$(az ad app create --display-name "Root Cause Console" \
  --web-redirect-uris https://rcc.company.com/api/auth/sso/callback \
  --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad sp create --id $APP
az ad app credential reset --id $APP --display-name rcc-sso --years 1 --query password -o tsv   # the client secret
echo "Issuer: https://login.microsoftonline.com/$TENANT/v2.0   Client ID: $APP"
```

| Field | Value |
|---|---|
| Issuer address | `https://login.microsoftonline.com/TENANT_ID/v2.0` |
| Client ID | The application (client) ID |
| Client secret | The secret printed above |

In the Azure portal, under the app's **Enterprise application > Properties**, set **Assignment required** to *Yes*, then assign the users or groups who may sign in. Otherwise everyone in your tenant can. The email the console sees is the user's `mail` attribute, so make sure users have one.

Note the secret's expiry date. When it expires, SSO sign-in fails until you paste a new one.

## Google Workspace

1. In Google Cloud Console, **APIs & Services > OAuth consent screen**: choose **Internal**, so only your Workspace users can sign in.
2. **Credentials > Create credentials > OAuth client ID**, type **Web application**, with the redirect address as an authorized redirect URI.

| Field | Value |
|---|---|
| Issuer address | `https://accounts.google.com` |
| Client ID, client secret | From the OAuth client |

## Okta

Create an **OIDC Web Application** with the redirect address as a sign-in redirect URI, and assign it to the right groups.

| Field | Value |
|---|---|
| Issuer address | `https://COMPANY.okta.com`, or `https://COMPANY.okta.com/oauth2/default` with a custom authorization server |
| Client ID, client secret | From the app's General tab |

## Keycloak

Create a client with **Client authentication** on and **Standard flow** enabled, and add the redirect address to **Valid redirect URIs**.

| Field | Value |
|---|---|
| Issuer address | `https://keycloak.company.com/realms/REALM` |
| Client ID, client secret | From the client's Credentials tab |

The console must be able to reach the issuer from its server. If you set `RCC_TARGET_ALLOWLIST`, include the provider's host names (for Entra ID, `login.microsoftonline.com` and `graph.microsoft.com`). A Keycloak with a private CA certificate isn't supported yet.

## Require single sign-on

Once SSO works, owners can tick **Require single sign-on for everyone** in **Organization > Security policies**. Password sign-in is then refused for everyone except the first account created at install, which stays as a break-glass account in case your provider is down.

> **Security:** Protect that break-glass account: a long, unique password kept in your vault, used only in an emergency. Every sign-in is in the audit trail, so check it for unexpected use.

## When sign-in fails

The person is sent back to the sign-in page with *Single sign-on didn’t finish. Try again, or contact your admin.* Common causes:

| Cause | Fix |
|---|---|
| Redirect address mismatch | Register exactly `RCC_PUBLIC_URL/api/auth/sso/callback` at your provider |
| The email's domain isn't listed | Add it to **Email domains** |
| The provider says the email isn't verified, or sends none | Make sure users have an email address at the provider |
| The account was deactivated | Reactivate it in **Organization > Members** |
| An account with that email is tied to another SSO identity | Remove the old account, or have the person use their original identity |
| The client secret expired | Create a new one and paste it |
| *Could not reach the identity provider* | The console's server can't reach the issuer: check the firewall and `RCC_TARGET_ALLOWLIST` |
