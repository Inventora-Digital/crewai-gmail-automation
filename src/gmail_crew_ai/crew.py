from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task, before_kickoff
from crewai_tools import FileReadTool
import json
import os
from typing import List, Dict, Any, Callable
from pydantic import SkipValidation
from datetime import date, datetime

from gmail_crew_ai.tools.gmail_tools import GetUnreadEmailsTool, SaveDraftTool, GmailOrganizeTool, GmailDeleteTool, EmptyTrashTool
from gmail_crew_ai.tools.slack_tool import SlackNotificationTool
from gmail_crew_ai.tools.date_tools import DateCalculationTool
from gmail_crew_ai.models import (
    CategorizedEmail,
    OrganizedEmail,
    EmailResponse,
    SlackNotification,
    EmailCleanupInfo,
    SimpleCategorizedEmail,
    EmailDetails,
    CategorizationBatch,
    OrganizationBatch,
    CleanupBatch,
    ResponsesBatch,
)

@CrewBase
class GmailCrewAi():
    """Crew that processes emails."""
    agents_config = 'config/agents.yaml'
    tasks_config = 'config/tasks.yaml'

    @before_kickoff
    def fetch_emails(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch emails before starting the crew and calculate ages."""
        print("Fetching emails before starting the crew...")
        # Get the email limit from inputs
        email_limit = inputs.get('email_limit', 5)
        print(f"Fetching {email_limit} emails...")
        # Create the output directory if it doesn't exist
        os.makedirs("output", exist_ok=True)
        # Use the GetUnreadEmailsTool directly
        email_tool = GetUnreadEmailsTool()
        email_tuples = email_tool._run(limit=email_limit)
        # Convert email tuples to EmailDetails objects with pre-calculated ages
        emails = []
        today = date.today()
        for email_tuple in email_tuples:
            email_detail = EmailDetails.from_email_tuple(email_tuple)
            # Calculate age if date is available
            if email_detail.date:
                try:
                    email_date_obj = datetime.strptime(email_detail.date, "%Y-%m-%d").date()
                    email_detail.age_days = (today - email_date_obj).days
                    print(f"Email date: {email_detail.date}, age: {email_detail.age_days} days")
                except Exception as e:
                    print(f"Error calculating age for email date {email_detail.date}: {e}")
                    email_detail.age_days = None
            emails.append(email_detail.dict())
        # Save emails to file
        with open('output/fetched_emails.json', 'w') as f:
            json.dump(emails, f, indent=2)
        print(f"Fetched and saved {len(emails)} emails to output/fetched_emails.json")
        return inputs

    llm = LLM(
        model="gemini/gemini-2.0-flash",
        api_key=os.getenv("GEMINI_API_KEY"),
    )

    @agent
    def categorizer(self) -> Agent:
        """The email categorizer agent."""
        return Agent(
            config=self.agents_config['categorizer'],
            tools=[FileReadTool()],
            llm=self.llm,
        )

    @agent
    def organizer(self) -> Agent:
        """The email organization agent."""
        return Agent(
            config=self.agents_config['organizer'],
            tools=[GmailOrganizeTool(), FileReadTool()],
            llm=self.llm,
        )

    @agent
    def response_generator(self) -> Agent:
        """The email response generator agent."""
        return Agent(
            config=self.agents_config['response_generator'],
            # Plan-only; no tools to avoid multiple tool calls during Pydantic parsing
            tools=[],
            llm=self.llm,
        )

    @agent
    def notifier(self) -> Agent:
        """The email notification agent."""
        return Agent(
            config=self.agents_config['notifier'],
            tools=[SlackNotificationTool()],
            llm=self.llm,
        )

    @agent
    def response_executor(self) -> Agent:
        """Executes response drafts using SaveDraftTool."""
        return Agent(
            config=self.agents_config.get('response_executor', self.agents_config['response_generator']),
            tools=[SaveDraftTool()],
            llm=self.llm,
        )

    @agent
    def cleaner(self) -> Agent:
        """The email cleanup agent."""
        return Agent(
            config=self.agents_config['cleaner'],
            # IMPORTANT: Avoid tools here to prevent multiple tool calls
            # during Pydantic parsing with Instructor. The cleanup task
            # returns structured data; execution tools can be run in a
            # separate step if needed.
            tools=[],
            llm=self.llm,
        )

    @agent
    def cleanup_executor(self) -> Agent:
        """Executes the cleanup plan by deleting emails and emptying trash."""
        return Agent(
            # Use a dedicated executor profile for imperative tool usage
            config=self.agents_config.get('cleanup_executor', self.agents_config['cleaner']),
            # tools=[GmailDeleteTool(), EmptyTrashTool()],
            tools=[GmailDeleteTool()],
            llm=self.llm,
        )

    @task
    def categorization_task(self) -> Task:
        """The email categorization task."""
        return Task(
            config=self.tasks_config['categorization_task'],
            output_pydantic=CategorizationBatch,
        )

    @task
    def organization_task(self) -> Task:
        """The email organization task."""
        return Task(
            config=self.tasks_config['organization_task'],
            output_pydantic=OrganizationBatch,
        )

    @task
    def response_task(self) -> Task:
        """The email response task."""
        return Task(
            config=self.tasks_config['response_task'],
            output_pydantic=ResponsesBatch,
        )

    @task
    def response_execute_task(self) -> Task:
        """Execute response drafts from the response plan."""
        return Task(
            config=self.tasks_config['response_execute_task'],
            agent=self.response_executor(),
        )

    @task
    def notification_task(self) -> Task:
        """The email notification task."""
        return Task(
            config=self.tasks_config['notification_task'],
            output_pydantic=SlackNotification,
        )

    @task
    def cleanup_task(self) -> Task:
        """The email cleanup task."""
        return Task(
            config=self.tasks_config['cleanup_task'],
            output_pydantic=CleanupBatch,
        )

    @task
    def cleanup_execute_task(self) -> Task:
        """Execute deletions from the cleanup plan and empty trash."""
        return Task(
            config=self.tasks_config['cleanup_execute_task'],
            # No output_pydantic here to allow multiple tool calls
            agent=self.cleanup_executor(),
        )

    @crew
    def crew(self) -> Crew:
        """Creates the email processing crew."""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )

    def _debug_callback(self, event_type, payload):
        """Debug callback for crew events."""
        if event_type == "task_start":
            print(f"DEBUG: Starting task: {payload.get('task_name')}")
        elif event_type == "task_end":
            print(f"DEBUG: Finished task: {payload.get('task_name')}")
            print(f"DEBUG: Task output type: {type(payload.get('output'))}")
            output = payload.get('output')
            if output:
                if isinstance(output, dict):
                    print(f"DEBUG: Output keys: {output.keys()}")
                    for key, value in output.items():
                        print(f"DEBUG: {key}: {value[:100] if isinstance(value, str) and len(value) > 100 else value}")
                elif isinstance(output, list):
                    print(f"DEBUG: Output list length: {len(output)}")
                    if output and len(output) > 0:
                        print(f"DEBUG: First item type: {type(output[0])}")
                        if isinstance(output[0], dict):
                            print(f"DEBUG: First item keys: {output[0].keys()}")
                else:
                    print(f"DEBUG: Output: {str(output)[:200]}...")
        elif event_type == "agent_start":
            print(f"DEBUG: Agent starting: {payload.get('agent_name')}")
        elif event_type == "agent_end":
            print(f"DEBUG: Agent finished: {payload.get('agent_name')}")
        elif event_type == "error":
            print(f"DEBUG: Error: {payload.get('error')}")

    def _validate_categorization_output(self, output):
        """Validate the categorization output before writing to file."""
        print(f"DEBUG: Validating categorization output: {output}")
        # If model returned the wrapper {"items": [...]}, unwrap it
        if isinstance(output, dict) and 'items' in output and isinstance(output['items'], list):
            print("DEBUG: Detected CategorizationBatch wrapper; unwrapping items")
            items = output['items']
        else:
            items = None
            if isinstance(output, dict):
                print("DEBUG: Single dict output detected, wrapping in list")
                items = [output]
            elif isinstance(output, str):
                try:
                    parsed = json.loads(output)
                    if isinstance(parsed, dict):
                        print("DEBUG: Parsed string to dict, wrapping in list")
                        items = [parsed]
                    elif isinstance(parsed, list):
                        print(f"DEBUG: Parsed string to list of length {len(parsed)}")
                        items = parsed
                    else:
                        print("DEBUG: Parsed string to unsupported type, returning empty list")
                        items = []
                except Exception as e:
                    print(f"WARNING: Unable to parse string output as JSON: {e}")
                    import re
                    json_pattern = r'\{.*\}'
                    match = re.search(json_pattern, output, re.DOTALL)
                    if match:
                        try:
                            json_str = match.group(0)
                            parsed = json.loads(json_str)
                            if isinstance(parsed, dict):
                                print("DEBUG: Successfully extracted and parsed JSON using regex, wrapping in list")
                                items = [parsed]
                            elif isinstance(parsed, list):
                                print(f"DEBUG: Successfully extracted and parsed JSON list using regex, length {len(parsed)}")
                                items = parsed
                            else:
                                print("DEBUG: Extracted JSON is unsupported type, returning empty list")
                                items = []
                        except Exception as e2:
                            print(f"WARNING: Failed to parse extracted JSON: {e2}")
                            items = []
                    else:
                        items = []
            elif isinstance(output, list):
                print(f"DEBUG: Output is already a list of length {len(output)}")
                items = output
            else:
                print("DEBUG: Output type unrecognized, returning empty list")
                items = []

        required_fields = ["email_id", "subject", "category", "priority", "required_action"]
        # Ensure each item is a dict and has required fields
        valid_items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                print(f"WARNING: Item at index {i} is not a dict, skipping")
                continue
            for field in required_fields:
                if field not in item:
                    item[field] = ""
            valid_items.append(item)
        # Fix placeholder values in each item if present
        for item in valid_items:
            if item.get("email_id") == "12345" and item.get("subject") == "Urgent Task Update":
                print("WARNING: Item contains placeholder values, trying to fix")
                try:
                    with open("output/fetched_emails.json", "r") as f:
                        fetched_emails = json.load(f)
                        if fetched_emails and len(fetched_emails) > 0:
                            real_email = fetched_emails[0]
                            item["email_id"] = real_email.get("email_id", "")
                            item["subject"] = real_email.get("subject", "")
                except Exception as e:
                    print(f"WARNING: Failed to fix placeholder values: {e}")
        return valid_items
