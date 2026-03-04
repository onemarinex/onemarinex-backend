def generate_hpid(passport_number: str, nationality: str, port: str) -> str:
    """
    Generate HPID using pattern: HP-{passport_number}-{NAT}-{PORT}
    Examples: HP-P1234567-IND-MUM, HP-P7654321-PHI-DUB
    """
    # Use upper case passport number
    id_part = str(passport_number).strip().upper() if passport_number else "XXXX"
    
    # First 3 chars of nationality
    nat_part = str(nationality)[:3].upper() if nationality else "GEN"
    
    # First 3 chars of port (stripping 'port_' prefix if present)
    port_part = str(port).replace("port_", "")[:3].upper() if port else "GEN"
    
    return f"HP-{id_part}-{nat_part}-{port_part}"
