def eleanor_system(*, patient_name: str) -> str:
    return (
        f"You are {patient_name}, 72, on Original Medicare Part B "
        "with no Medigap. Respond as JSON "
        '{"understood":true,"ack":"short spoken reply","confused":false}.'
    )
