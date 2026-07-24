# IAM Policy Notes

`iam_policy.json` is valid AWS IAM JSON, so it cannot include inline comments.
This file explains why each statement exists and why its scope is shaped that
way.

| Sid | Why It Is Needed | Scope Rationale |
| --- | --- | --- |
| `Ec2Discovery` | Lets deploy find the default VPC, public subnet, route tables, existing Kern instances, security groups, volumes, and instance attributes. | EC2 describe APIs do not support meaningful resource-level scoping, so they use `Resource: "*"` and stay read-only. |
| `CreateTaggedKernResources` | Lets deploy create the EC2 instance, data volumes, and security group only when the request includes the Kern ownership tag. | Uses `Resource: "*"` because these create APIs authorize multiple resource checks, but requires `aws:RequestTag/kern-host=true` so created resources must be tagged at creation. |
| `UseEc2CreateDependencies` | Lets `RunInstances` and `CreateSecurityGroup` pass EC2's checks for referenced resources such as the Ubuntu AMI, subnet, network interface, and VPC. | These dependencies are not newly created Kern resources and cannot be bounded by the Kern request tag, so they are listed by resource type and have no tag condition. |
| `RunInstancesWithKernSecurityGroups` | Lets `RunInstances` use the selected existing security group. | The security group must already have `aws:ResourceTag/kern-host=true`, which prevents launching with arbitrary security groups. |
| `TagOnlyDuringKernResourceCreation` | Lets AWS apply tag specifications during `RunInstances`, `CreateVolume`, and `CreateSecurityGroup`. | Requires `aws:RequestTag/kern-host=true` and `ec2:CreateAction` so the permission only covers tag-on-create, not standalone tagging of arbitrary existing resources. |
| `ManageOnlyKernResources` | Lets deploy read console output, attach volumes, mark durable data volumes not to delete on instance termination, update security group rules, start, stop, or terminate instances, and delete cleanup resources. | Uses `Resource: "*"` because these EC2 APIs have mixed resource behavior, but requires `aws:ResourceTag/kern-host=true` so only Kern-owned resources can be managed. |
| `UbuntuAmiLookup` | Lets deploy resolve the Canonical Ubuntu SSM parameter used to find the base AMI. | Scoped to Canonical public SSM parameters; it does not grant broad SSM parameter access. |
| `AwsLogin` | Lets an IAM user or federated role exchange an authenticated AWS console session for temporary CLI credentials with `aws login`. | Grants only the two AWS Sign-In OAuth actions and scopes them to local-development public clients. It does not add AWS service permissions to the identity. |

The policy intentionally uses both tag condition types:

- `aws:RequestTag/kern-host` controls tags supplied in a create request.
- `aws:ResourceTag/kern-host` controls existing resources that already
  carry the Kern ownership tag.
