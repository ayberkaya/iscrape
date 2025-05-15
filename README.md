# iScrape - Web Scraping SaaS Platform

iScrape is a powerful web scraping platform that allows users to extract data from websites using customizable templates. The platform offers a freemium model with different subscription tiers to cater to various user needs.

## Features

- **Easy-to-use Interface**: No coding required to start scraping
- **Customizable Templates**: Pre-built and custom templates for different use cases
- **Multiple Export Formats**: Export data in CSV, JSON, and Excel formats
- **AI-Powered Template Creation**: Generate templates using AI
- **API Access**: Programmatic access to scraping capabilities
- **Background Processing**: Asynchronous job processing
- **Subscription Management**: Integrated with Stripe for payments

## Subscription Tiers

### Free Tier

- 10 daily scraping operations
- Basic templates
- CSV export format
- Community support

### Pro Tier ($29.99/month)

- 100 daily scraping operations
- All templates
- AI template creation
- Priority support
- Multiple export formats (CSV, JSON, Excel)

### Enterprise Tier ($99.99/month)

- Unlimited scraping operations
- Custom templates
- API access
- 24/7 support
- Custom integrations

## Setup Instructions

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourusername/iscrape.git
   cd iscrape
   ```

2. **Create and activate a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**

   - Copy `.env.example` to `.env`
   - Fill in your credentials:
     - Stripe API keys
     - OpenAI API key
     - Email configuration
     - Other required settings

5. **Initialize the database**

   ```bash
   flask db init
   flask db migrate
   flask db upgrade
   ```

6. **Start Redis server** (required for background tasks)

   ```bash
   redis-server
   ```

7. **Start Celery worker** (in a new terminal)

   ```bash
   celery -A app.celery worker --loglevel=info
   ```

8. **Run the application**
   ```bash
   flask run
   ```

## Development

### Project Structure

```
iscrape/
├── app.py              # Main application file
├── requirements.txt    # Python dependencies
├── .env               # Environment variables
├── templates/         # HTML templates
├── static/           # Static files (CSS, JS, images)
├── migrations/       # Database migrations
└── tests/           # Test files
```

### Running Tests

```bash
python -m pytest
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## Security

- All passwords are hashed using SHA-256
- API keys are securely stored
- HTTPS is enforced in production
- Regular security audits are performed

## Support

For support, please:

1. Check the [documentation](https://docs.iscrape.com)
2. Search [existing issues](https://github.com/yourusername/iscrape/issues)
3. Create a new issue if needed

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
