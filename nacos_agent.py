"""Publish or discover an AgentScope A2A endpoint in Nacos 3.3."""

import argparse
import json
import os
import signal
import time
from uuid import uuid4

import httpx


AGENTS = "/v3/client/ai/agents"
ENDPOINTS = f"{AGENTS}/endpoints"


def request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"{method} {path}: {payload['code']} {payload.get('message', '')}")
    return payload


def login(client: httpx.Client, username: str, password: str) -> float:
    result = request(client, "POST", "/v3/auth/user/login", data={
        "username": username, "password": password,
    })
    client.headers["accessToken"] = result["accessToken"]
    return time.monotonic() + result["tokenTtl"] / 2


def register(
    nacos: httpx.Client, args: argparse.Namespace, password: str, refresh_at: float,
) -> None:
    with httpx.Client(timeout=5, trust_env=False) as agent:
        response = agent.get(args.card_url)
        response.raise_for_status()
        card = response.json()
        interface = next(
            (item for item in card["supportedInterfaces"]
             if item["protocolBinding"] == "JSONRPC" and item["protocolVersion"] == "1.0"),
            None,
        )
        if interface is None:
            raise RuntimeError("Agent Card must advertise an A2A 1.0 JSONRPC interface.")

        identity = {"namespaceId": args.namespace, "agentName": card["name"]}
        version = card["version"]
        if args.publish:
            definition = request(nacos, "POST", AGENTS, data={
                **identity,
                "version": version,
                "description": card["description"],
                "autoSubmit": "true",
                "callInterfaces": json.dumps([{
                    "protocol": "a2a",
                    "protocolVersion": "1.0",
                    "descriptorMediaType": "application/json",
                    "nativeDescriptor": card,
                    "endpointSourceOrder": ["RUNTIME", "DECLARED"],
                }]),
            })["data"]
            if definition["status"] != "online":
                raise RuntimeError(f"Agent version is {definition['status']}. Publish it in Nacos first.")
            print(f"Published {card['name']} {version}", flush=True)

        request(nacos, "GET", AGENTS, params={**identity, "version": version, "protocol": "a2a"})
        headers = {"X-Nacos-Client-Id": str(uuid4()), "Request-Module": "AI"}
        target = {**identity, "protocol": "a2a"}
        lifetime = request(nacos, "POST", ENDPOINTS, headers=headers, data={
            **target,
            "runtimeVersion": version,
            "versionRange": f"[{version}]",
            "endpoints": json.dumps([{"uri": interface["url"], "transport": "JSONRPC"}]),
        })["data"]
        print(f"Registered {card['name']} at {interface['url']}", flush=True)
        try:
            while True:
                time.sleep(lifetime["heartbeatIntervalMillis"] / 1000)
                agent.get(args.card_url).raise_for_status()
                if time.monotonic() >= refresh_at:
                    refresh_at = login(nacos, args.username, password)
                lifetime = request(nacos, "PUT", f"{ENDPOINTS}/heartbeat", headers=headers)["data"]
        except KeyboardInterrupt:
            request(nacos, "DELETE", ENDPOINTS, headers=headers, params=target)
            print(f"Deregistered {card['name']} at {interface['url']}", flush=True)
            raise


def discover(nacos: httpx.Client, args: argparse.Namespace) -> None:
    result = request(nacos, "GET", AGENTS, params={
        "namespaceId": args.namespace,
        "agentName": args.name,
        "version": args.version,
        "protocol": "a2a",
        "endpointSource": "RUNTIME",
        "transport": "JSONRPC",
    })["data"]
    for interface in result["callInterfaces"]:
        for group in interface["endpointSets"]:
            for endpoint in group["endpoints"]:
                if endpoint["healthy"]:
                    print(endpoint["uri"])
                    return
    raise RuntimeError(f"No healthy runtime endpoint for {args.name} {args.version}.")


def interrupt(_signal, _frame) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nacos-url", default="http://127.0.0.1:8848/nacos")
    parser.add_argument("--namespace", default="public")
    parser.add_argument("--username", default="nacos")
    commands = parser.add_subparsers(dest="command", required=True)
    publisher = commands.add_parser("register", help="Register the endpoint and send heartbeats")
    publisher.add_argument("--card-url", default="http://127.0.0.1:9999/.well-known/agent-card.json")
    publisher.add_argument("--publish", action="store_true", help="Publish the Agent definition first")
    consumer = commands.add_parser("discover", help="Print a healthy A2A runtime URL")
    consumer.add_argument("--name", default="Friday")
    consumer.add_argument("--version", default="1.0.0")
    args = parser.parse_args()

    password = os.environ.get("NACOS_PASSWORD")
    if not password:
        parser.error("Set NACOS_PASSWORD before running this command.")
    signal.signal(signal.SIGTERM, interrupt)
    with httpx.Client(base_url=args.nacos_url, timeout=8, trust_env=False) as nacos:
        try:
            refresh_at = login(nacos, args.username, password)
            if args.command == "register":
                register(nacos, args, password, refresh_at)
            else:
                discover(nacos, args)
        except KeyboardInterrupt:
            pass
        except (httpx.HTTPError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
