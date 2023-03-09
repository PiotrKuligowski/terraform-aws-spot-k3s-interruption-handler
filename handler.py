import boto3
import time
import os

asg_client = boto3.client('autoscaling', region_name=os.getenv('REGION'))
ec2_client = boto3.client('ec2', region_name=os.getenv('REGION'))
ssm_client = boto3.client('ssm', region_name=os.getenv('REGION'))


def describe_instance(instance_id):
    return ec2_client.describe_instances(
        InstanceIds=[instance_id]
    )['Reservations'][0]['Instances'][0]

def get_tag_value(tags, key):
    for tag in tags:
        if tag['Key'] == key:
            return tag['Value']
    return None

def get_command_by_status(command_id, status='Success'):
    return ssm_client.list_command_invocations(
        CommandId=command_id,
        Filters=[
            {
                'key': 'Status',
                'value': status
            },
        ],
        Details=True
    )['CommandInvocations']

def wait_until_command_complete(command_id):
    max_timeout = 60
    while len(get_command_by_status(command_id)) == 0:
        time.sleep(1)
        max_timeout -= 1
        if max_timeout <= 0:
            break

def get_ssm_param_value(param_name):
    return ssm_client.get_parameter(
        Name=param_name
    )['Parameter']['Value']

def wait_until_new_master_ready(current_master_id):
    max_timeout = 120
    param_name = os.getenv('CURRENT_MASTER_ID_PARAM_NAME')
    while current_master_id == get_ssm_param_value(param_name):
        time.sleep(1)
        max_timeout -= 1
        if max_timeout <= 0:
            break
    return get_ssm_param_value(param_name)

def handle_interrupted_node(instance_id, asg_name, node_name):
    print("Node has been interrupted")

    print("Detaching interrupted node and adding replacement node")
    asg_client.detach_instances(
        InstanceIds=[instance_id],
        AutoScalingGroupName=asg_name,
        ShouldDecrementDesiredCapacity=False
    )

    print(f"Draining {node_name} node")
    drain_response = ssm_client.send_command(
        DocumentName="AWS-RunShellScript",
        Parameters={'commands':[f"kubectl drain {node_name} --ignore-daemonsets --delete-emptydir-data"]},
        InstanceIds=[get_ssm_param_value(os.getenv('CURRENT_MASTER_ID_PARAM_NAME'))]
    )
    wait_until_command_complete(drain_response['Command']['CommandId'])

    print(f"Removing {node_name} node from cluster")
    delete_response = ssm_client.send_command(
        DocumentName="AWS-RunShellScript",
        Parameters={'commands':[f"kubectl delete node {node_name}"]},
        InstanceIds=[get_ssm_param_value(os.getenv('CURRENT_MASTER_ID_PARAM_NAME'))]
    )
    wait_until_command_complete(delete_response['Command']['CommandId'])

    print(f"Terminating {instance_id} node")
    ec2_client.terminate_instances(InstanceIds=[instance_id])
    # Tool running in cluster should now even the load on all nodes

def handle_interrupted_control_plane(instance_id, asg_name, node_name):
    print("Control plane has been interrupted")

    print("Detaching interrupted control plane and adding replacement node")
    asg_client.detach_instances(
        InstanceIds=[instance_id],
        AutoScalingGroupName=asg_name,
        ShouldDecrementDesiredCapacity=False
    )

    new_master_id = wait_until_new_master_ready(instance_id)
    instance_describe = describe_instance(new_master_id)
    private_ip = instance_describe["PrivateIpAddress"]

    print(f"Updating {new_master_id} nginx config")
    update_response = ssm_client.send_command(
        DocumentName="AWS-RunShellScript",
        Parameters={
            'commands': [
                "sudo sed -i -e 's/[0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}\\.[0-9]\\{1,3\\}" + f"/{private_ip}/g' /etc/nginx/nginx.conf",
                "sudo systemctl restart nginx"
            ]
        },
        InstanceIds=[get_ssm_param_value(os.getenv('CURRENT_NLB_ID_PARAM_NAME'))]
    )
    wait_until_command_complete(update_response['Command']['CommandId'])

    print(f"Draining {node_name} node")
    drain_response = ssm_client.send_command(
        DocumentName="AWS-RunShellScript",
        Parameters={'commands':[f"kubectl drain {node_name} --ignore-daemonsets --delete-emptydir-data"]},
        InstanceIds=[get_ssm_param_value(os.getenv('CURRENT_MASTER_ID_PARAM_NAME'))]
    )
    wait_until_command_complete(drain_response['Command']['CommandId'])

    print(f"Removing {node_name} node from cluster")
    delete_response = ssm_client.send_command(
        DocumentName="AWS-RunShellScript",
        Parameters={'commands':[f"kubectl delete node {node_name}"]},
        InstanceIds=[get_ssm_param_value(os.getenv('CURRENT_MASTER_ID_PARAM_NAME'))]
    )
    wait_until_command_complete(delete_response['Command']['CommandId'])
    # Kill the node
    ec2_client.terminate_instances(InstanceIds=[instance_id])

def lambda_handler(event, context):
    instance_id = event['detail']['instance-id']
    instance_describe = describe_instance(instance_id)
    tags = instance_describe['Tags']

    asg_name = get_tag_value(tags, 'aws:autoscaling:groupName')
    k8s_node_name = instance_describe['PrivateDnsName'].split('.')[0]

    if not asg_name:
        print("Interrupted instance is not part of any autoscaling group, returning")
        return {'statusCode': 409}

    project = os.getenv('PROJECT')
    name = get_tag_value(tags, 'Name')

    if f"{project}-node" in name:
        handle_interrupted_node(instance_id, asg_name, k8s_node_name)

    if f"{project}-master" in name:
        handle_interrupted_control_plane(instance_id, asg_name, k8s_node_name)

    return {
        'statusCode' : 200,
        'body': 'result'
    }