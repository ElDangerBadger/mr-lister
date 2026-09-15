"""Local security-boundary checks; no SDK, network, or credential access."""

import json
import unittest
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent


class JudgeTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((DIRECTORY / "template.json").read_text())
        self.resources = self.template["Resources"]

    def test_only_the_federation_resource_targets_the_existing_pool(self):
        expected = {
            "JudgeUserPool": "AWS::Cognito::UserPool",
            "JudgeBrokerClient": "AWS::Cognito::UserPoolClient",
            "JudgeUserPoolDomain": "AWS::Cognito::UserPoolDomain",
            "JudgeBrokerClientSecret": "AWS::SecretsManager::Secret",
            "JudgeIdentityProvider": "AWS::Cognito::UserPoolIdentityProvider",
        }
        self.assertEqual({key: value["Type"] for key, value in self.resources.items()}, expected)
        for logical, resource in self.resources.items():
            props = resource["Properties"]
            if "UserPoolId" in props:
                expected_pool = (
                    "PrimaryUserPoolId" if logical == "JudgeIdentityProvider" else "JudgeUserPool"
                )
                self.assertEqual(props["UserPoolId"], {"Ref": expected_pool})
        self.assertNotIn("Transform", self.template)

    def test_judge_password_directory_does_not_relax_the_seller_pool(self):
        judge = self.resources["JudgeUserPool"]["Properties"]
        self.assertEqual(judge["MfaConfiguration"], "OFF")
        self.assertEqual(judge["UserPoolTier"], "LITE")
        self.assertEqual(judge["AdminCreateUserConfig"], {"AllowAdminCreateUserOnly": True})
        self.assertEqual(
            judge["AccountRecoverySetting"]["RecoveryMechanisms"],
            [{"Name": "admin_only", "Priority": 1}],
        )
        self.assertNotIn("LambdaConfig", judge)
        self.assertGreaterEqual(judge["Policies"]["PasswordPolicy"]["MinimumLength"], 14)
        primary = json.loads((DIRECTORY.parent / "template.json").read_text())
        self.assertEqual(
            primary["Resources"]["SellerUserPool"]["Properties"]["MfaConfiguration"], "ON"
        )

    def test_confidential_client_returns_codes_only_to_the_primary_broker(self):
        client = self.resources["JudgeBrokerClient"]["Properties"]
        self.assertTrue(client["GenerateSecret"])
        self.assertEqual(client["AllowedOAuthFlows"], ["code"])
        self.assertEqual(client["AllowedOAuthScopes"], ["openid", "email"])
        self.assertEqual(
            client["CallbackURLs"], [{"Fn::Sub": "${PrimarySignInOrigin}/oauth2/idpresponse"}]
        )
        self.assertEqual(client["LogoutURLs"], [{"Fn::Sub": "${ApplicationOrigin}/judge/"}])
        self.assertEqual(client["SupportedIdentityProviders"], ["COGNITO"])
        self.assertEqual(
            self.resources["JudgeUserPoolDomain"]["Properties"]["ManagedLoginVersion"], 1
        )

    def test_secret_is_empty_until_the_operator_binds_a_generated_client_secret(self):
        secret = self.resources["JudgeBrokerClientSecret"]["Properties"]
        self.assertNotIn("SecretString", secret)
        self.assertNotIn("GenerateSecretString", secret)
        self.assertEqual(self.template["Parameters"]["EnableFederation"]["Default"], "false")
        self.assertEqual(self.template["Parameters"]["BrokerSecretVersionId"]["Default"], "")
        rule = self.template["Rules"]["FederationRequiresPopulatedSecretVersion"]
        self.assertEqual(
            rule["RuleCondition"], {"Fn::Equals": [{"Ref": "EnableFederation"}, "true"]}
        )
        self.assertEqual(
            rule["Assertions"][0]["Assert"],
            {"Fn::Not": [{"Fn::Equals": [{"Ref": "BrokerSecretVersionId"}, ""]}]},
        )
        idp = self.resources["JudgeIdentityProvider"]
        self.assertEqual(idp["Condition"], "FederationEnabled")
        self.assertEqual(
            idp["Properties"]["ProviderDetails"]["client_secret"],
            {
                "Fn::Sub": (
                    "{{resolve:secretsmanager:${JudgeBrokerClientSecret}:"
                    "SecretString:client_secret::${BrokerSecretVersionId}}}"
                )
            },
        )
        # No unsupported ClientSecret GetAtt or value-bearing secret output.
        self.assertNotIn('"ClientSecret"', json.dumps(self.template))
        outputs = self.template["Outputs"]
        self.assertEqual(
            outputs["JudgeBrokerClientSecretArn"]["Value"], {"Ref": "JudgeBrokerClientSecret"}
        )
        self.assertNotIn("resolve:secretsmanager", json.dumps(outputs))

    def test_broker_maps_identity_attributes_without_granting_authority(self):
        idp = self.resources["JudgeIdentityProvider"]["Properties"]
        self.assertEqual(idp["ProviderName"], "MrListerJudge")
        self.assertEqual(idp["ProviderType"], "OIDC")
        self.assertEqual(
            idp["AttributeMapping"], {"email": "email", "email_verified": "email_verified"}
        )
        self.assertEqual(idp["ProviderDetails"]["client_id"], {"Ref": "JudgeBrokerClient"})
        self.assertEqual(
            idp["ProviderDetails"]["oidc_issuer"],
            {"Fn::Sub": "https://cognito-idp.${AWS::Region}.${AWS::URLSuffix}/${JudgeUserPool}"},
        )
        self.assertNotIn("IdpIdentifiers", idp)
        self.assertNotIn("cognito:groups", json.dumps(idp))

    def test_existing_authority_parameters_must_be_explicit(self):
        for key in (
            "PrimaryUserPoolId",
            "PrimarySignInOrigin",
            "ApplicationOrigin",
            "JudgeDomainPrefix",
            "EnvironmentName",
        ):
            self.assertNotIn("Default", self.template["Parameters"][key])
        for resource in self.resources.values():
            self.assertNotIn("ServiceToken", resource["Properties"])


if __name__ == "__main__":
    unittest.main()
