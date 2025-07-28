import json
import os
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Pre-signup Lambda trigger for Cognito to validate email domains.
    
    Args:
        event: The event from Cognito pre-signup trigger
        context: Lambda context
        
    Returns:
        The event object to be returned to Cognito
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Get the user's email from the event
    email = event['request']['userAttributes'].get('email', '')
    
    if not email:
        logger.warning("No email found in the request")
        return event
    
    # Extract domain from email
    domain = email.split('@')[-1].lower()
    logger.info(f"Email domain: {domain}")
    
    # Get the allowed domains from the environment variable
    allowed_domains = get_allowed_domains()
    
    # If no allowed domains are configured, allow all domains
    if not allowed_domains:
        logger.info("No allowed domains configured, allowing all domains")
        return event
    
    # Check if the domain is allowed
    if domain in allowed_domains:
        logger.info(f"Domain {domain} is allowed")
        return event
    else:
        logger.warning(f"Domain {domain} is not in the allowed list: {allowed_domains}")
        # Deny the signup by raising an exception
        raise Exception(f"Email domain '{domain}' is not allowed. Please use an email with one of these domains: {', '.join(allowed_domains)}")

def get_allowed_domains():
    """
    Get the list of allowed domains from the environment variable.
    
    Returns:
        List of allowed domains or empty list if not configured
    """
    try:
        # Get allowed domains from environment variable
        allowed_domains_str = os.environ.get('ALLOWED_DOMAINS', '')
        
        if not allowed_domains_str:
            logger.warning("ALLOWED_DOMAINS environment variable not set or empty")
            return []
        
        # Parse the comma-separated list of domains
        allowed_domains = [domain.strip().lower() for domain in allowed_domains_str.split(',') if domain.strip()]
        logger.info(f"Loaded allowed domains: {allowed_domains}")
        
        return allowed_domains
    except Exception as e:
        logger.warning(f"Error parsing allowed domains: {str(e)}")
        # If there's an error parsing the domains, allow all domains
        return []
