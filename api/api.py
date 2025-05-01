import os
import logging
import json
import subprocess
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
from starlette.responses import StreamingResponse
import google.generativeai as genai

from api.rag import RAG
from api.data_pipeline import count_tokens, get_file_content, get_local_repo_structure

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Get API keys from environment variables
google_api_key = os.environ.get('GOOGLE_API_KEY')

# Configure Google Generative AI
if google_api_key:
    genai.configure(api_key=google_api_key)
else:
    logger.warning("GOOGLE_API_KEY not found in environment variables")

# Initialize FastAPI app
app = FastAPI(
    title="Streaming API",
    description="API for streaming chat completions"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Models for the API
class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str

class ChatCompletionRequest(BaseModel):
    """
    Model for requesting a chat completion.
    """
    repo_url: str = Field(..., description="URL of the repository to query")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    filePath: Optional[str] = Field(None, description="Optional path to a file in the repository to include in the prompt")
    github_token: Optional[str] = Field(None, description="GitHub personal access token for private repositories")
    gitlab_token: Optional[str] = Field(None, description="GitLab personal access token for private repositories")

@app.post("/chat/completions/stream")
async def chat_completions_stream(request: ChatCompletionRequest):
    """Stream a chat completion response directly using Google Generative AI"""
    try:
        # Check if request contains very large input
        input_too_large = False
        if request.messages and len(request.messages) > 0:
            last_message = request.messages[-1]
            if hasattr(last_message, 'content') and last_message.content:
                tokens = count_tokens(last_message.content)
                logger.info(f"Request size: {tokens} tokens")
                if tokens > 8000:
                    logger.warning(f"Request exceeds recommended token limit ({tokens} > 7500)")
                    input_too_large = True

        # Create a new RAG instance for this request
        try:
            request_rag = RAG()

            # Determine which access token to use based on the repository URL
            access_token = None
            if "github.com" in request.repo_url and request.github_token:
                access_token = request.github_token
                logger.info("Using GitHub token for authentication")
            elif "gitlab.com" in request.repo_url and request.gitlab_token:
                access_token = request.gitlab_token
                logger.info("Using GitLab token for authentication")

            request_rag.prepare_retriever(request.repo_url, access_token)
            logger.info(f"Retriever prepared for {request.repo_url}")
        except Exception as e:
            logger.error(f"Error preparing retriever: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Error preparing retriever: {str(e)}")

        # Get the last user message
        if not request.messages or len(request.messages) == 0:
            raise HTTPException(status_code=400, detail="No messages provided")

        last_message = request.messages[-1]
        if last_message.role != "user":
            raise HTTPException(status_code=400, detail="Last message must be from the user")

        # Process previous messages to build conversation history
        for i in range(0, len(request.messages) - 1, 2):
            if i + 1 < len(request.messages):
                user_msg = request.messages[i]
                assistant_msg = request.messages[i + 1]

                if user_msg.role == "user" and assistant_msg.role == "assistant":
                    request_rag.memory.add_dialog_turn(
                        user_query=user_msg.content,
                        assistant_response=assistant_msg.content
                    )

        # Get the query from the last message
        query = last_message.content

        # Only retrieve documents if input is not too large
        context_text = ""
        retrieved_documents = None

        if not input_too_large:
            try:
                # If filePath exists, modify the query for RAG to focus on the file
                rag_query = query
                if request.filePath:
                    # Use the file path to get relevant context about the file
                    rag_query = f"Contexts related to {request.filePath}"
                    logger.info(f"Modified RAG query to focus on file: {request.filePath}")

                # Try to perform RAG retrieval
                try:
                    # This will use the actual RAG implementation
                    response, retrieved_documents = request_rag(rag_query)

                    if retrieved_documents and retrieved_documents[0].documents:
                        # Format context for the prompt
                        context_text = "\n\n".join([doc.text for doc in retrieved_documents[0].documents])
                        logger.info(f"Retrieved {len(retrieved_documents[0].documents)} documents")
                    else:
                        logger.warning("No documents retrieved from RAG")
                except Exception as e:
                    logger.error(f"Error in RAG retrieval: {str(e)}")
                    # Continue without RAG if there's an error

            except Exception as e:
                logger.error(f"Error retrieving documents: {str(e)}")
                context_text = ""

        # Create system prompt with repository information
        repo_url = request.repo_url
        repo_name = repo_url.split("/")[-1] if "/" in repo_url else repo_url

        # Determine repository type
        repo_type = "GitHub"
        if "gitlab.com" in repo_url:
            repo_type = "GitLab"

        system_prompt = f"""<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You provide direct, concise, and accurate information about code repositories.
You NEVER start responses with markdown headers or code fences.
</role>

<guidelines>
- Answer the user's question directly without ANY preamble or filler phrases
- DO NOT start with preambles like "Okay, here's a breakdown" or "Here's an explanation"
- DO NOT start with markdown headers like "## Analysis of..." or any file path references
- DO NOT start with ```markdown code fences
- DO NOT end your response with ``` closing fences
- DO NOT start by repeating or acknowledging the question
- JUST START with the direct answer to the question

<example_of_what_not_to_do>
```markdown
## Analysis of `adalflow/adalflow/datasets/gsm8k.py`

This file contains...
```
</example_of_what_not_to_do>

- Format your response with proper markdown including headings, lists, and code blocks WITHIN your answer
- For code analysis, organize your response with clear sections
- Think step by step and structure your answer logically
- Start with the most relevant information that directly addresses the user's query
- Be precise and technical when discussing code
</guidelines>

<style>
- Use concise, direct language
- Prioritize accuracy over verbosity
- When showing code, include line numbers and file paths when relevant
- Use markdown formatting to improve readability
</style>
"""

        # Format conversation history
        conversation_history = ""
        for turn_id, turn in request_rag.memory().items():
            if not isinstance(turn_id, int) and hasattr(turn, 'user_query') and hasattr(turn, 'assistant_response'):
                conversation_history += f"<turn>\n<user>{turn.user_query.query_str}</user>\n<assistant>{turn.assistant_response.response_str}</assistant>\n</turn>\n"

        # Create the prompt with context
        prompt = f"{system_prompt}\n\n"

        if conversation_history:
            prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"

        # Check if filePath is provided and fetch file content if it exists
        file_content = ""
        if request.filePath:
            try:
                # Determine which access token to use based on the repository URL
                access_token = None
                if "github.com" in request.repo_url and request.github_token:
                    access_token = request.github_token
                elif "gitlab.com" in request.repo_url and request.gitlab_token:
                    access_token = request.gitlab_token

                file_content = get_file_content(request.repo_url, request.filePath, access_token)
                logger.info(f"Successfully retrieved content for file: {request.filePath}")
                # Add file content to the prompt after conversation history
                prompt += f"<currentFileContent path=\"{request.filePath}\">\n{file_content}\n</currentFileContent>\n\n"
            except Exception as e:
                logger.error(f"Error retrieving file content: {str(e)}")
                # Continue without file content if there's an error

        # Only include context if it's not empty
        if context_text.strip():
            prompt += f"<context>\n{context_text}\n</context>\n\n"
        else:
            # Add a note that we're skipping RAG due to size constraints or because it's the isolated API
            logger.info("No context available from RAG")
            prompt += "<note>Answering without retrieval augmentation.</note>\n\n"

        prompt += f"<query>\n{query}\n</query>\n\nAssistant: "

        # Initialize Google Generative AI model
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",  # Using the preferred model
            generation_config={
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 40
            }
        )

        # Create a streaming response
        async def response_stream():
            try:
                # Generate streaming response
                response = model.generate_content(prompt, stream=True)

                # Stream the response
                for chunk in response:
                    if hasattr(chunk, 'text'):
                        yield chunk.text

                # Make sure to resolve the response to avoid gRPC warnings
                # try:
                #     response.resolve()
                # except Exception as e:
                #     logger.warning(f"Could not resolve response: {e}")

            except Exception as e:
                logger.error(f"Error in streaming response: {str(e)}")
                error_message = str(e)

                # Check for token limit errors
                if "maximum context length" in error_message or "token limit" in error_message or "too many tokens" in error_message:
                    # If we hit a token limit error, try again without context
                    logger.warning("Token limit exceeded, retrying without context")
                    try:
                        # Create a simplified prompt without context
                        simplified_prompt = f"{system_prompt}\n\n"
                        if conversation_history:
                            simplified_prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"

                        # Include file content in the fallback prompt if it was retrieved
                        if request.filePath and file_content:
                            simplified_prompt += f"<currentFileContent path=\"{request.filePath}\">\n{file_content}\n</currentFileContent>\n\n"

                        simplified_prompt += "<note>Answering without retrieval augmentation due to input size constraints.</note>\n\n"
                        simplified_prompt += f"<query>\n{query}\n</query>\n\nAssistant: "

                        # Try again with simplified prompt
                        fallback_response = model.generate_content(simplified_prompt, stream=True)

                        # Stream the fallback response
                        for chunk in fallback_response:
                            if hasattr(chunk, 'text'):
                                yield chunk.text

                        # Resolve the fallback response
                        # try:
                        #     fallback_response.resolve()
                        # except Exception as e2:
                        #     logger.warning(f"Could not resolve fallback response: {e2}")

                    except Exception as e2:
                        logger.error(f"Error in fallback streaming response: {str(e2)}")
                        yield f"\nI apologize, but your request is too large for me to process. Please try a shorter query or break it into smaller parts."
                else:
                    # For other errors, return the error message
                    yield f"\nError: {error_message}"

        # Return streaming response
        return StreamingResponse(response_stream(), media_type="text/event-stream")

    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Error in streaming chat completion: {str(e)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

class RepoStructureRequest(BaseModel):
    """
    Model for requesting repository structure.
    """
    repo_url: str = Field(..., description="URL of the repository to get structure for")
    github_token: Optional[str] = Field(None, description="GitHub personal access token for private repositories")
    gitlab_token: Optional[str] = Field(None, description="GitLab personal access token for private repositories")

@app.post("/repo/structure")
async def get_repository_structure(request: RepoStructureRequest):
    """
    Get the file structure of a repository.
    This endpoint will first try to use the GitHub/GitLab API, and if that fails,
    it will fall back to using the local filesystem.
    """
    try:
        # Determine which access token to use based on the repository URL
        access_token = None
        if "github.com" in request.repo_url and request.github_token:
            access_token = request.github_token
            logger.info("Using GitHub token for authentication")
        elif "gitlab.com" in request.repo_url and request.gitlab_token:
            access_token = request.gitlab_token
            logger.info("Using GitLab token for authentication")

        # Parse repository URL to extract owner and repo
        repo_url = request.repo_url
        repo_type = "github"  # Default to GitHub
        owner = ""
        repo = ""

        if "github.com" in repo_url:
            # GitHub URL format
            parts = repo_url.replace('https://github.com/', '').split('/')
            owner = parts[0] or ''
            repo = parts[1] or ''
            repo_type = "github"
        elif "gitlab.com" in repo_url:
            # GitLab URL format
            parts = repo_url.replace('https://gitlab.com/', '').split('/')

            # GitLab can have nested groups, so the repo is the last part
            # and the owner/group is everything before that
            if len(parts) >= 2:
                repo = parts[-1] or ''
                owner = parts[0] or ''
                repo_type = "gitlab"
        else:
            # Handle owner/repo format (assume GitHub by default)
            parts = repo_url.split('/')
            owner = parts[0] or ''
            repo = parts[1] or ''

        # Clean values and remove .git suffix if present
        owner = owner.strip()
        repo = repo.strip()
        if repo.endswith('.git'):
            repo = repo[:-4]

        if not owner or not repo:
            raise ValueError("Invalid repository format. Could not extract owner and repo name.")

        # Try to get the repository structure using the GitHub/GitLab API first
        try:
            file_tree_data = ""

            if repo_type == "github":
                # Try to get the tree data for common branch names
                for branch in ['main', 'master']:
                    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
                    headers = {'Accept': 'application/vnd.github.v3+json'}

                    # Add GitHub token if available
                    if access_token:
                        headers['Authorization'] = f"Bearer {access_token}"

                    logger.info(f"Fetching repository structure from GitHub API for branch: {branch}")
                    try:
                        # Use subprocess to make the request
                        curl_cmd = ["curl", "-s"]
                        for key, value in headers.items():
                            curl_cmd.extend(["-H", f"{key}: {value}"])
                        curl_cmd.append(api_url)

                        result = subprocess.run(
                            curl_cmd,
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                        response_data = json.loads(result.stdout.decode("utf-8"))

                        if "tree" in response_data:
                            # Convert tree data to a string representation
                            file_tree_data = "\n".join([
                                item["path"] for item in response_data["tree"]
                                if item.get("type") == "blob"
                            ])
                            logger.info(f"Successfully fetched repository structure from GitHub API for branch: {branch}")
                            break
                    except Exception as e:
                        logger.warning(f"Error fetching GitHub repository structure for branch {branch}: {e}")

            elif repo_type == "gitlab":
                # Try to get the file tree for common branch names in GitLab
                encoded_project_path = f"{owner}%2F{repo}"

                for branch in ['main', 'master']:
                    api_url = f"https://gitlab.com/api/v4/projects/{encoded_project_path}/repository/tree?recursive=true&ref={branch}&per_page=100"
                    headers = {'Content-Type': 'application/json'}

                    # Add GitLab token if available
                    if access_token:
                        headers['PRIVATE-TOKEN'] = access_token

                    logger.info(f"Fetching repository structure from GitLab API for branch: {branch}")
                    try:
                        # Use subprocess to make the request
                        curl_cmd = ["curl", "-s"]
                        for key, value in headers.items():
                            curl_cmd.extend(["-H", f"{key}: {value}"])
                        curl_cmd.append(api_url)

                        result = subprocess.run(
                            curl_cmd,
                            check=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                        response_data = json.loads(result.stdout.decode("utf-8"))

                        if isinstance(response_data, list) and len(response_data) > 0:
                            # Convert files data to a string representation
                            file_tree_data = "\n".join([
                                item["path"] for item in response_data
                                if item.get("type") == "blob"
                            ])
                            logger.info(f"Successfully fetched repository structure from GitLab API for branch: {branch}")
                            break
                    except Exception as e:
                        logger.warning(f"Error fetching GitLab repository structure for branch {branch}: {e}")

            # If we couldn't get the file tree data from the API, fall back to the local filesystem
            if not file_tree_data:
                logger.info("API-based repository structure fetch failed or returned empty result. Falling back to local filesystem.")
                file_tree_data = get_local_repo_structure(repo_url, access_token)

            return {"file_tree": file_tree_data}

        except Exception as api_error:
            # If the API approach fails, fall back to the local filesystem
            logger.warning(f"Error fetching repository structure from API: {api_error}. Falling back to local filesystem.")
            file_tree_data = get_local_repo_structure(repo_url, access_token)
            return {"file_tree": file_tree_data}

    except Exception as e:
        error_msg = f"Error fetching repository structure: {str(e)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/")
async def root():
    """Root endpoint to check if the API is running"""
    return {
        "message": "Welcome to Streaming API",
        "version": "1.0.0",
        "endpoints": {
            "Chat": [
                "POST /chat/completions/stream - Streaming chat completion",
            ],
            "Repository": [
                "POST /repo/structure - Get repository file structure",
            ]
        }
    }
